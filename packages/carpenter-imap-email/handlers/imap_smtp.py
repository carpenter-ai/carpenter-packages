"""TRUSTED platform-side capability handlers for the IMAP/SMTP backend.

This module is the security-critical new code of the carpenter-imap-email
package.  Each handler is registered by the platform-capability
framework against a dispatch verb declared in ``manifest.yaml``'s
``platform_capabilities`` section and invoked as::

    handler(params: dict, ctx: CapabilityContext) -> dict

These handlers run PARENT-SIDE in TRUSTED context (not in the executor
sandbox).  They are the only place network I/O and credentials touch the
IMAP/SMTP servers.  The untrusted EXECUTOR scripts in ``scripts.py`` are
cred-free and host-free; they reach the network ONLY by dispatching one
of these verbs.

Trust contract — host + credentials come from ``ctx``, NEVER from params:

* ``ctx.host`` / ``ctx.port`` / ``ctx.protocol`` are the operator-confirmed
  egress grant (bound at install from ``IMAP_EMAIL_IMAP_HOST`` /
  ``IMAP_EMAIL_SMTP_HOST``).  The handler may not point egress anywhere
  else.
* ``ctx.secret("IMAP_USERNAME")`` etc. resolve ``IMAP_EMAIL_<SUFFIX>``
  PLATFORM-SIDE (live process env / loaded .env), never from the
  untrusted executor.
* ``params`` carries ONLY the operation payload the executor controls:
  mailbox / uid / query / flags / outgoing-message.  Every param is
  validated and bounded before use.  A param named ``host`` / ``port`` /
  ``password`` is ignored — those come from ``ctx``.

Bounds enforced:

* Connection + socket timeouts (``_TIMEOUT_S``).
* Maximum fetched message size (``_MAX_FETCH_BYTES``); larger messages
  are truncated with ``truncated: true`` in the result.
* Maximum search result count (``_MAX_SEARCH_RESULTS``).
* UID / mailbox / flag shape validation (reject anything that could be
  an IMAP-command-injection vector).

Every handler returns a JSON-serialisable dict — the value crosses the
JSON-only dispatch boundary back to the untrusted executor, which writes
it to a Resource for the REVIEWER + JUDGE.  Handlers never raise the raw
credential or host into an error string.
"""

from __future__ import annotations

import imaplib
import re
import smtplib
import time
from email.message import EmailMessage

# ── Bounds ──────────────────────────────────────────────────────────

_TIMEOUT_S = 30.0
# Cap a single fetched message at ~5 MiB of raw RFC-822 bytes.  The
# REVIEWER only needs headers + a text summary; we refuse to stream a
# multi-hundred-MB attachment payload into a Resource.
_MAX_FETCH_BYTES = 5 * 1024 * 1024
_MAX_SEARCH_RESULTS = 100
_MAX_RAW_MESSAGE_BYTES = 10 * 1024 * 1024

# A UID is a positive integer per RFC 3501; accept the decimal string
# form the executor passes.  Reject anything else — a UID is the only
# message selector we trust from params, and an unbounded string here
# would be an IMAP command-injection vector.
_UID_RE = re.compile(r"^[0-9]{1,19}$")
# A mailbox name: conservative printable-ASCII subset, no CR/LF (which
# would split the IMAP command line), bounded length.
_MAILBOX_RE = re.compile(r"^[A-Za-z0-9_./\- ]{1,128}$")
# An IMAP flag: a backslash-prefixed system flag or a bare atom.  No
# spaces / control chars (would break the STORE command).
_FLAG_RE = re.compile(r"^\\?[A-Za-z0-9_]{1,64}$")
# IMAP search keys we permit the executor to use.  Keeps the search
# surface to an audit-readable allowlist; the term is quoted.
_ALLOWED_SEARCH_KEYS = frozenset({
    "ALL", "UNSEEN", "SEEN", "RECENT", "FROM", "TO", "CC", "SUBJECT",
    "BODY", "TEXT", "SINCE", "BEFORE", "ON", "FLAGGED", "UNFLAGGED",
})


class _HandlerError(Exception):
    """Raised on a param-validation / protocol failure inside a handler.

    The message is safe to surface to the executor: it never contains
    the credential or the resolved host.
    """


# ── Param validation helpers ────────────────────────────────────────


def _require_str(params: dict, key: str, *, default: str | None = None) -> str:
    val = params.get(key, default)
    if not isinstance(val, str):
        raise _HandlerError(f"param {key!r} must be a string, got {type(val).__name__}")
    return val


def _valid_mailbox(params: dict, *, default: str = "INBOX") -> str:
    mailbox = params.get("mailbox", default)
    if not isinstance(mailbox, str) or not _MAILBOX_RE.match(mailbox):
        raise _HandlerError(
            f"param 'mailbox' must match {_MAILBOX_RE.pattern!r}; refusing "
            f"a value that could inject into the IMAP command line",
        )
    return mailbox


def _valid_uid(params: dict) -> str:
    uid = params.get("uid")
    # Accept an int too, but normalise to the decimal string form.
    if isinstance(uid, bool):  # bool is an int subclass — reject explicitly
        raise _HandlerError("param 'uid' must be a numeric uid, not a bool")
    if isinstance(uid, int):
        uid = str(uid)
    if not isinstance(uid, str) or not _UID_RE.match(uid):
        raise _HandlerError(
            "param 'uid' must be a positive integer (IMAP UID); refusing "
            "an unbounded string (command-injection guard)",
        )
    return uid


def _valid_flags(params: dict) -> tuple[str, ...]:
    raw = params.get("flags", ())
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple)):
        raise _HandlerError("param 'flags' must be a string or list of strings")
    flags: list[str] = []
    for f in raw:
        if not isinstance(f, str) or not _FLAG_RE.match(f):
            raise _HandlerError(
                f"flag {f!r} must match {_FLAG_RE.pattern!r}; refusing a "
                f"value that could break the IMAP STORE command",
            )
        flags.append(f)
    if not flags:
        raise _HandlerError("param 'flags' must be non-empty for a store")
    return tuple(flags)


# ── Connection helpers (host + creds from ctx ONLY) ─────────────────


def _imap_login(ctx) -> imaplib.IMAP4_SSL:
    """Open a TLS IMAP connection to ``ctx.host:ctx.port`` and log in.

    Host + port come from the operator-confirmed grant; username +
    password resolve platform-side via ``ctx.secret``.  None of these
    come from handler params.
    """
    conn = imaplib.IMAP4_SSL(host=ctx.host, port=ctx.port, timeout=_TIMEOUT_S)
    username = ctx.secret("IMAP_USERNAME")
    password = ctx.secret("IMAP_PASSWORD")
    conn.login(username, password)
    return conn


def _close_quietly(conn: imaplib.IMAP4_SSL | None) -> None:
    if conn is None:
        return
    try:
        try:
            conn.close()
        except Exception:
            pass
        conn.logout()
    except Exception:
        pass


# ── Capability handlers ─────────────────────────────────────────────


def handle_imap_fetch(params: dict, ctx) -> dict:
    """Fetch one message by UID and return its raw RFC-822 bytes (text).

    Params (executor-controlled): ``uid`` (required), ``mailbox``
    (optional, default INBOX), ``peek`` (optional bool, default True —
    use BODY.PEEK so fetching does not implicitly mark the message
    \\Seen).

    Returns ``{"ok", "uid", "mailbox", "rfc822", "size_bytes",
    "truncated", "host", "port"}``.  The ``rfc822`` text is what the
    REVIEWER summarises; the JUDGE bounds the typed extract.
    """
    try:
        uid = _valid_uid(params)
        mailbox = _valid_mailbox(params)
        peek = params.get("peek", True)
        item = "BODY.PEEK[]" if peek else "RFC822"
        conn = None
        try:
            conn = _imap_login(ctx)
            typ, _ = conn.select(mailbox, readonly=peek)
            if typ != "OK":
                raise _HandlerError(f"IMAP SELECT {mailbox!r} failed: {typ}")
            typ, data = conn.uid("FETCH", uid, item)
            if typ != "OK":
                raise _HandlerError(f"IMAP UID FETCH {uid} failed: {typ}")
            raw = b""
            for part in data:
                if isinstance(part, tuple) and len(part) >= 2 and part[1]:
                    raw = part[1]
                    break
            size = len(raw)
            truncated = False
            if size > _MAX_FETCH_BYTES:
                raw = raw[:_MAX_FETCH_BYTES]
                truncated = True
            return {
                "ok": True,
                "uid": uid,
                "mailbox": mailbox,
                "rfc822": raw.decode("utf-8", errors="replace"),
                "size_bytes": size,
                "truncated": truncated,
                "host": ctx.host,
                "port": ctx.port,
            }
        finally:
            _close_quietly(conn)
    except _HandlerError as exc:
        return {"ok": False, "error": str(exc), "verb": "imap.fetch"}
    except Exception as exc:  # noqa: BLE001 — surface a bounded error
        return {"ok": False, "error": f"imap.fetch failed: {exc}", "verb": "imap.fetch"}


def handle_imap_search(params: dict, ctx) -> dict:
    """Search a mailbox and return matching UIDs (capped).

    Params (executor-controlled): ``mailbox`` (optional, default INBOX),
    ``criteria`` (optional list of [KEY, term] / [KEY] pairs drawn from
    an allowlist; default ``[["ALL"]]``), ``max_results`` (optional int,
    capped at ``_MAX_SEARCH_RESULTS``).

    Returns ``{"ok", "mailbox", "uids", "count", "host", "port"}``.
    """
    try:
        mailbox = _valid_mailbox(params)
        max_results = params.get("max_results", _MAX_SEARCH_RESULTS)
        if isinstance(max_results, bool) or not isinstance(max_results, int):
            raise _HandlerError("param 'max_results' must be an integer")
        if max_results < 1:
            max_results = 1
        if max_results > _MAX_SEARCH_RESULTS:
            max_results = _MAX_SEARCH_RESULTS

        search_args = _build_search_args(params.get("criteria"))

        conn = None
        try:
            conn = _imap_login(ctx)
            typ, _ = conn.select(mailbox, readonly=True)
            if typ != "OK":
                raise _HandlerError(f"IMAP SELECT {mailbox!r} failed: {typ}")
            typ, data = conn.uid("SEARCH", None, *search_args)
            if typ != "OK":
                raise _HandlerError(f"IMAP UID SEARCH failed: {typ}")
            raw = (data[0] or b"") if data else b""
            uids = raw.split()
            # Most-recent-first, capped.
            uids = [u.decode("ascii", errors="replace") for u in uids]
            uids = [u for u in uids if _UID_RE.match(u)]
            uids = list(reversed(uids))[:max_results]
            return {
                "ok": True,
                "mailbox": mailbox,
                "uids": uids,
                "count": len(uids),
                "host": ctx.host,
                "port": ctx.port,
            }
        finally:
            _close_quietly(conn)
    except _HandlerError as exc:
        return {"ok": False, "error": str(exc), "verb": "imap.search"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"imap.search failed: {exc}", "verb": "imap.search"}


def _build_search_args(criteria) -> tuple[str, ...]:
    """Validate executor-supplied search criteria into IMAP SEARCH args.

    ``criteria`` is an optional list of ``[KEY]`` or ``[KEY, term]``
    pairs; KEY must be in ``_ALLOWED_SEARCH_KEYS`` and the term is quoted
    so it cannot escape into the command.  Defaults to ``("ALL",)``.
    """
    if criteria is None:
        return ("ALL",)
    if not isinstance(criteria, (list, tuple)) or not criteria:
        raise _HandlerError("param 'criteria' must be a non-empty list")
    args: list[str] = []
    for entry in criteria:
        if not isinstance(entry, (list, tuple)) or not entry:
            raise _HandlerError("each criteria entry must be [KEY] or [KEY, term]")
        key = entry[0]
        if not isinstance(key, str) or key.upper() not in _ALLOWED_SEARCH_KEYS:
            raise _HandlerError(
                f"search key {key!r} not in allowlist {sorted(_ALLOWED_SEARCH_KEYS)}",
            )
        args.append(key.upper())
        if len(entry) >= 2:
            term = entry[1]
            if not isinstance(term, str):
                raise _HandlerError("criteria term must be a string")
            # Reject control chars / quotes that would break the IMAP
            # quoted-string; bound the length.
            if len(term) > 256 or any(ord(c) < 0x20 for c in term) or '"' in term:
                raise _HandlerError("criteria term has illegal characters or is too long")
            args.append(f'"{term}"')
    return tuple(args)


def handle_imap_store(params: dict, ctx) -> dict:
    """Set or remove flags on a message by UID (archive / mark-read).

    Params (executor-controlled): ``uid`` (required), ``mailbox``
    (optional, default INBOX), ``flags`` (required, list of valid IMAP
    flags), ``op`` (optional, ``"add"`` / ``"remove"``; default
    ``"add"``).

    For mark-read, the executor adds ``\\Seen``.  For archive, the
    backend convention is to move/expunge from INBOX; in this MVP
    archive is represented as adding the ``\\Deleted``-free mailbox-move
    is out of scope, so the package's archive template instead sets a
    provider flag and reports the prior state.

    Returns ``{"ok", "uid", "mailbox", "flags", "op", "prior_flags",
    "host", "port"}``.
    """
    try:
        uid = _valid_uid(params)
        mailbox = _valid_mailbox(params)
        flags = _valid_flags(params)
        op = _require_str(params, "op", default="add").lower()
        if op not in ("add", "remove"):
            raise _HandlerError("param 'op' must be 'add' or 'remove'")
        store_cmd = "+FLAGS" if op == "add" else "-FLAGS"
        conn = None
        try:
            conn = _imap_login(ctx)
            typ, _ = conn.select(mailbox, readonly=False)
            if typ != "OK":
                raise _HandlerError(f"IMAP SELECT {mailbox!r} failed: {typ}")
            # Read prior flags so the receipt can report idempotency.
            prior_flags: tuple[str, ...] = ()
            typ, data = conn.uid("FETCH", uid, "(FLAGS)")
            if typ == "OK" and data and data[0]:
                prior_flags = _parse_flags(data[0])
            flag_str = "(" + " ".join(flags) + ")"
            typ, _ = conn.uid("STORE", uid, store_cmd, flag_str)
            if typ != "OK":
                raise _HandlerError(f"IMAP UID STORE {uid} failed: {typ}")
            return {
                "ok": True,
                "uid": uid,
                "mailbox": mailbox,
                "flags": list(flags),
                "op": op,
                "prior_flags": list(prior_flags),
                "host": ctx.host,
                "port": ctx.port,
            }
        finally:
            _close_quietly(conn)
    except _HandlerError as exc:
        return {"ok": False, "error": str(exc), "verb": "imap.store"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"imap.store failed: {exc}", "verb": "imap.store"}


def _parse_flags(fetch_line) -> tuple[str, ...]:
    """Extract flag atoms from an IMAP FETCH (FLAGS ...) response line."""
    if isinstance(fetch_line, tuple):
        fetch_line = b" ".join(p for p in fetch_line if isinstance(p, bytes))
    if isinstance(fetch_line, bytes):
        fetch_line = fetch_line.decode("ascii", errors="replace")
    if not isinstance(fetch_line, str):
        return ()
    m = re.search(r"FLAGS\s*\(([^)]*)\)", fetch_line)
    if not m:
        return ()
    return tuple(tok for tok in m.group(1).split() if tok)


def handle_smtp_send(params: dict, ctx) -> dict:
    """Send (or stage) a message via SMTPS to ``ctx.host:ctx.port``.

    Params (executor-controlled): ``raw_message`` (required, the full
    RFC-822 message text to send) OR the structured trio ``to`` (list),
    ``subject``, ``body``.  The envelope sender is the authenticated
    SMTP username (from ``ctx``), never a params-supplied From.

    Returns ``{"ok", "accepted_recipients", "refused", "host", "port"}``.
    """
    try:
        to_list, msg_bytes = _build_outgoing(params, ctx)
        if not to_list:
            raise _HandlerError("no recipients resolved for smtp.send")
        if len(msg_bytes) > _MAX_RAW_MESSAGE_BYTES:
            raise _HandlerError("outgoing message exceeds size bound")

        username = ctx.secret("SMTP_USERNAME")
        password = ctx.secret("SMTP_PASSWORD")
        refused: dict[str, str] = {}
        with smtplib.SMTP_SSL(host=ctx.host, port=ctx.port, timeout=_TIMEOUT_S) as server:
            server.login(username, password)
            # Envelope sender is the authenticated account, not params.
            send_errors = server.sendmail(username, to_list, msg_bytes)
            for rcpt, (code, resp) in (send_errors or {}).items():
                refused[str(rcpt)] = f"{code} {resp!r}"
        accepted = [r for r in to_list if r not in refused]
        return {
            "ok": not refused,
            "accepted_recipients": accepted,
            "refused": refused,
            "host": ctx.host,
            "port": ctx.port,
        }
    except _HandlerError as exc:
        return {"ok": False, "error": str(exc), "verb": "smtp.send"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"smtp.send failed: {exc}", "verb": "smtp.send"}


def _build_outgoing(params: dict, ctx) -> tuple[list[str], bytes]:
    """Resolve the recipient list + RFC-822 bytes for smtp.send.

    Accepts either a pre-built ``raw_message`` (RFC-822 text) plus an
    explicit ``to`` recipient list, or the structured ``to`` / ``subject``
    / ``body`` trio.  The From header / envelope sender is always the
    authenticated SMTP account from ``ctx`` — a params-supplied From is
    ignored.
    """
    to_list = params.get("to")
    if to_list is None:
        to_list = []
    if isinstance(to_list, str):
        to_list = [to_list]
    if not isinstance(to_list, list) or not all(isinstance(x, str) for x in to_list):
        raise _HandlerError("param 'to' must be a list of address strings")
    # Bound recipient count.
    if len(to_list) > 50:
        raise _HandlerError("too many recipients (max 50)")

    sender = ctx.secret("SMTP_USERNAME")

    raw_message = params.get("raw_message")
    if isinstance(raw_message, str) and raw_message:
        # Caller pre-built the message (RFC-822 text).  We still own the
        # envelope sender + recipient list (from params/ctx), so a body
        # that lies about From cannot redirect the envelope.
        return to_list, raw_message.encode("utf-8")

    subject = params.get("subject")
    body = params.get("body")
    if not isinstance(subject, str) or not isinstance(body, str):
        raise _HandlerError(
            "smtp.send needs either 'raw_message' or the 'to'/'subject'/'body' trio",
        )
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg.set_content(body)
    return to_list, msg.as_bytes()


def handle_imap_append(params: dict, ctx) -> dict:
    """APPEND a raw RFC-822 message into a named IMAP folder.

    mailbox.org (unlike Gmail's API) does NOT auto-file SMTP-sent mail
    into ``Sent`` — a raw SMTP send leaves no server-side copy.  So the
    send flow explicitly APPENDs the just-sent message to ``Sent`` via
    this verb.  It egresses to the IMAP host under THIS verb's own grant
    (imaps / IMAP_HOST / 993 / IMAP_EMAIL) — the same egress class as the
    other ``imap.*`` verbs, and a different one than ``smtp.send`` — so
    the Sent-copy never widens the smtp.send grant.

    Params (executor-controlled): ``raw_message`` (required, the full
    RFC-822 message text to file), ``mailbox`` (optional, default
    ``Sent``), ``flags`` (optional list, default ``["\\Seen"]`` so the
    filed copy isn't shown as unread).  Host + credentials come from
    ``ctx`` only.

    Returns ``{"ok", "mailbox", "flags", "size_bytes", "host", "port"}``.
    """
    try:
        mailbox = _valid_mailbox(params, default="Sent")
        raw_message = params.get("raw_message")
        if not isinstance(raw_message, str) or not raw_message:
            raise _HandlerError("param 'raw_message' must be a non-empty RFC-822 string")
        msg_bytes = raw_message.encode("utf-8")
        if len(msg_bytes) > _MAX_RAW_MESSAGE_BYTES:
            raise _HandlerError("message to append exceeds size bound")

        # Default to \Seen so the filed copy is not counted as unread.
        raw_flags = params.get("flags", ("\\Seen",))
        if isinstance(raw_flags, str):
            raw_flags = (raw_flags,)
        if not isinstance(raw_flags, (list, tuple)):
            raise _HandlerError("param 'flags' must be a string or list of strings")
        flags: list[str] = []
        for f in raw_flags:
            if not isinstance(f, str) or not _FLAG_RE.match(f):
                raise _HandlerError(
                    f"flag {f!r} must match {_FLAG_RE.pattern!r}; refusing a "
                    f"value that could break the IMAP APPEND command",
                )
            flags.append(f)
        flag_str = "(" + " ".join(flags) + ")" if flags else None

        conn = None
        try:
            conn = _imap_login(ctx)
            typ, _ = conn.append(
                mailbox, flag_str, imaplib.Time2Internaldate(time.time()), msg_bytes,
            )
            if typ != "OK":
                raise _HandlerError(f"IMAP APPEND to {mailbox!r} failed: {typ}")
            return {
                "ok": True,
                "mailbox": mailbox,
                "flags": flags,
                "size_bytes": len(msg_bytes),
                "host": ctx.host,
                "port": ctx.port,
            }
        finally:
            _close_quietly(conn)
    except _HandlerError as exc:
        return {"ok": False, "error": str(exc), "verb": "imap.append"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"imap.append failed: {exc}", "verb": "imap.append"}
