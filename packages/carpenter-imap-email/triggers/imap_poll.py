"""IMAP inbound UID-polling trigger (carpenter-imap-email, v0.2.0).

Periodically polls one or more IMAP folders for newly-arrived messages
and emits one ``email.received`` event per new message.  Subscribers
(the package's manifest declares an ``email.received`` subscription that
fans out to a triage arc tree) do the full untrusted fetch + REVIEWER +
JUDGE pipeline.  This trigger never fetches a message body; the payload
is minimal (provider UID, folder, account) — enough for the triage arc
to fetch+classify inside the trust pipeline.

This is the IMAP analogue of carpenter-gmail's ``gmail_poll.py``.  It
mirrors that trigger's structure (in-process ``PollableTrigger``,
config-overridable cadence, concurrent-poll CAS guard, backoff window,
backpressure cap, watermark persisted in ``package_state``), but the
watermark model is different because IMAP has no Gmail-style monotonic
history cursor:

* **Per-folder UID watermark, UIDVALIDITY-aware.**  RFC 3501 UIDs are
  only monotonic *within a (mailbox, UIDVALIDITY) generation*.  When a
  server renumbers a mailbox it bumps ``UIDVALIDITY`` and the old UIDs
  become meaningless.  So for each watched folder we persist BOTH the
  highest-seen UID and the UIDVALIDITY it was seen under.  On each tick
  we read the folder's current ``UIDVALIDITY``; if it changed we RESET
  (re-baseline to the current max UID, emit nothing) rather than
  re-emitting the whole mailbox under stale UIDs.
* **First run with no watermark**: record the folder's current max UID
  (+ its UIDVALIDITY) and emit nothing.  Backfill of pre-existing mail
  is explicitly out of scope.
* On a normal tick we ``UID SEARCH UID <watermark+1>:*`` for new UIDs,
  emit one ``email.received`` per new message (capped), then advance the
  watermark via CAS so an overlapping poll cannot regress it.

Credentials + host are resolved **platform-side** the same way the
package's TRUSTED capability handlers resolve them.  The capability
loader resolves a verb's egress host from ``{credential_ref}_{host_from}``
via :func:`carpenter.packages.capabilities.resolve_package_secret`, and
``CapabilityContext.secret`` resolves usernames/passwords through the
identical resolver.  This trigger runs in TRUSTED platform context (it is
a package-shipped trigger, not an untrusted executor script), so it calls
``resolve_package_secret(self.source_package, "EMAIL_IMAP_HOST")`` etc.
directly — the same env / per-package ``.env`` / config layering as
``ctx.secret``.  Nothing comes from an untrusted executor.

**Folder policy (the Junk decision):** the trigger watches ``INBOX`` by
default.  The watched-folder set is configurable via the trigger's
``folders`` config list, and the operator MAY add ``Junk`` — but we do
NOT silently watch Junk (spam) by default.  See ``kb/email/inbound-triage.md``.
"""

from __future__ import annotations

import imaplib
import logging
import re
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

from carpenter.core.engine.triggers.base import PollableTrigger
from carpenter.packages.capabilities import resolve_package_secret


logger = logging.getLogger(__name__)


# Package state key shapes.  Watermarks are per-folder; we namespace the
# folder name into the key so two watched folders never collide.  Values
# are stored as ``{"uid": int, "uidvalidity": int}`` JSON blobs.
def _watermark_key(folder: str) -> str:
    return f"imap_watermark::{folder}"


_KEY_BACKOFF_UNTIL = "imap_poll_backoff_until"
_KEY_POLL_IN_PROGRESS = "imap_poll_in_progress"

# Tunables.  Cadence is config-overridable; the rest are constants by
# design — exposing them widens the attack surface for little gain.
_DEFAULT_CADENCE_SECONDS = 15 * 60
_CADENCE_FLOOR_SECONDS = 60
_IMAP_TIMEOUT_SECONDS = 30.0
_MAX_EMITS_PER_POLL = 25
_BACKOFF_SECONDS_ERROR = 60 * 60  # 1 hour on connection / auth error
_DEFAULT_FOLDERS = ("INBOX",)

# A UID is a positive integer per RFC 3501.  We never interpolate a UID
# we did not derive from the server's own SEARCH response, but bound it
# anyway as defence in depth.
_UID_RE = re.compile(r"^[0-9]{1,19}$")
# Conservative mailbox-name shape (no CR/LF that could split the IMAP
# command line); mirrors handlers/imap_smtp.py's _MAILBOX_RE.
_MAILBOX_RE = re.compile(r"^[A-Za-z0-9_./\- ]{1,128}$")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _coerce_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class ImapPollTrigger(PollableTrigger):
    """Polls IMAP folder(s) for new UIDs and emits ``email.received``.

    Config keys (all optional, all set via the manifest's
    ``triggers[].config``):

    * ``cadence_seconds``: int seconds between polls.  Default 900
      (15 min), floored at 60.
    * ``event_type``: the event_type string to emit.  Default
      ``"email.received"``.
    * ``folders``: list of folder names to watch.  Default ``["INBOX"]``.
      The operator MAY add ``"Junk"`` here, but we never watch Junk by
      default (spam should not auto-trigger triage).

    Host + credentials come from the package's declared ``kind: env``
    credential (env_key_prefix ``EMAIL``), resolved platform-side via
    :func:`carpenter.packages.capabilities.resolve_package_secret` —
    identical to how the trusted capability handlers resolve them.
    """

    @classmethod
    def trigger_type(cls) -> str:
        return "imap_poll"

    def __init__(
        self,
        name: str,
        config: dict,
        *,
        source_package: str | None = None,
        package_state: Any = None,
        package_vectors: Any = None,
    ) -> None:
        super().__init__(
            name,
            config,
            source_package=source_package,
            package_state=package_state,
            package_vectors=package_vectors,
        )
        cadence = _coerce_int(config.get("cadence_seconds", _DEFAULT_CADENCE_SECONDS))
        if cadence is None:
            logger.warning(
                "ImapPollTrigger %r: cadence_seconds=%r is not an int; "
                "falling back to %d",
                name, config.get("cadence_seconds"), _DEFAULT_CADENCE_SECONDS,
            )
            cadence = _DEFAULT_CADENCE_SECONDS
        if cadence < _CADENCE_FLOOR_SECONDS:
            logger.warning(
                "ImapPollTrigger %r: cadence_seconds=%d below the %ds "
                "floor; clamping",
                name, cadence, _CADENCE_FLOOR_SECONDS,
            )
            cadence = _CADENCE_FLOOR_SECONDS
        self.cadence_seconds = cadence
        self.event_type = str(config.get("event_type", "email.received"))
        self.folders = self._normalise_folders(config.get("folders"))
        # Credential prefix.  Matches the manifest's kind:env credential
        # (env_key_prefix EMAIL) and the capability grants' credential_ref.
        self._cred_prefix = "EMAIL"
        # In-process disable flag (set on a hard auth failure) — cleared
        # only by a daemon restart, mirroring gmail_poll's model.
        self._disabled_in_process = False
        # Heartbeat cadence guard.
        self._last_poll_at: datetime | None = None

    @staticmethod
    def _normalise_folders(raw: Any) -> tuple[str, ...]:
        """Validate the configured watched-folder list.

        Defaults to ``("INBOX",)``.  Rejects names that could inject into
        the IMAP command line.  Never adds Junk implicitly.
        """
        if raw is None:
            return _DEFAULT_FOLDERS
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            logger.warning(
                "ImapPollTrigger: folders=%r is not a list; using default "
                "INBOX",
                raw,
            )
            return _DEFAULT_FOLDERS
        out: list[str] = []
        for f in raw:
            if not isinstance(f, str) or not _MAILBOX_RE.match(f):
                logger.warning(
                    "ImapPollTrigger: ignoring invalid folder name %r", f,
                )
                continue
            if f not in out:
                out.append(f)
        return tuple(out) if out else _DEFAULT_FOLDERS

    # ── Credential resolution (platform-side, package-scoped) ─────────

    def _secret(self, suffix: str) -> str | None:
        """Resolve ``EMAIL_<suffix>`` platform-side for THIS package.

        Identical resolver to ``CapabilityContext.secret`` /
        the capability loader's host resolution.  Returns None when
        unset (the trigger treats that as "not configured yet").
        """
        if not self.source_package:
            return None
        return resolve_package_secret(
            self.source_package, f"{self._cred_prefix}_{suffix}",
        )

    def _resolve_connection(self) -> tuple[str, int, str, str] | None:
        """Return ``(host, port, username, password)`` or None if unset."""
        host = self._secret("IMAP_HOST")
        username = self._secret("IMAP_USERNAME")
        password = self._secret("IMAP_PASSWORD")
        if not host or not username or not password:
            return None
        port = _coerce_int(self._secret("IMAP_PORT")) or 993
        return host, port, username, password

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """First-run init: baseline each watched folder's max UID.

        If credentials are not yet set (operator installs before
        configuring the mailbox) we defer baselining to the first
        ``check()`` — non-fatal, mirroring gmail_poll.
        """
        if self.package_state is None:
            logger.warning(
                "ImapPollTrigger %r: no package_state handle; cannot "
                "persist watermarks.  Polling disabled in-process.",
                self.name,
            )
            self._disabled_in_process = True
            return
        conn_info = self._resolve_connection()
        if conn_info is None:
            logger.info(
                "ImapPollTrigger %r: EMAIL_IMAP_* credentials not set at "
                "start; first-run baseline deferred until they are",
                self.name,
            )
            return
        try:
            self._baseline_missing_folders(conn_info)
        except Exception:
            logger.exception(
                "ImapPollTrigger %r: first-run baseline failed; will retry "
                "on next poll",
                self.name,
            )

    def stop(self) -> None:
        """Best-effort: clear the poll-in-progress flag on shutdown."""
        if self.package_state is None:
            return
        try:
            self.package_state.delete(_KEY_POLL_IN_PROGRESS)
        except Exception:
            logger.debug(
                "ImapPollTrigger %r: stop() failed to clear poll_in_progress",
                self.name, exc_info=True,
            )

    # ── Heartbeat entry point ────────────────────────────────────────

    def check(self) -> None:
        """Called each heartbeat.  Decides whether to do a real poll."""
        if self._disabled_in_process or self.package_state is None:
            return
        now = _now_utc()
        if self._last_poll_at is not None:
            if (now - self._last_poll_at).total_seconds() < self.cadence_seconds:
                return
        backoff_until = self._backoff_until()
        if backoff_until is not None and now < backoff_until:
            logger.debug(
                "ImapPollTrigger %r: in backoff until %s; skipping",
                self.name, backoff_until.isoformat(),
            )
            return
        if not self._claim_poll_slot():
            logger.debug(
                "ImapPollTrigger %r: another poll in progress; skipping",
                self.name,
            )
            return
        try:
            self._do_poll()
        finally:
            self._release_poll_slot()
            self._last_poll_at = _now_utc()

    # ── Poll-slot CAS guard ──────────────────────────────────────────

    def _claim_poll_slot(self) -> bool:
        state = self.package_state
        if state is None:
            return False
        try:
            # cas(expected_version=0, ...) is the insert-if-absent form.
            return bool(state.cas(_KEY_POLL_IN_PROGRESS, 0, True))
        except Exception:
            logger.exception(
                "ImapPollTrigger %r: CAS on poll_in_progress failed",
                self.name,
            )
            return False

    def _release_poll_slot(self) -> None:
        state = self.package_state
        if state is None:
            return
        try:
            state.delete(_KEY_POLL_IN_PROGRESS)
        except Exception:
            logger.debug(
                "ImapPollTrigger %r: failed to clear poll_in_progress",
                self.name, exc_info=True,
            )

    # ── Backoff window ───────────────────────────────────────────────

    def _backoff_until(self) -> datetime | None:
        state = self.package_state
        if state is None:
            return None
        try:
            raw = state.get(_KEY_BACKOFF_UNTIL)
        except Exception:
            return None
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            logger.warning(
                "ImapPollTrigger %r: malformed backoff value %r; clearing",
                self.name, raw,
            )
            try:
                state.delete(_KEY_BACKOFF_UNTIL)
            except Exception:
                pass
            return None

    def _set_backoff(self, seconds: int) -> None:
        state = self.package_state
        if state is None:
            return
        until = _now_utc() + timedelta(seconds=int(seconds))
        try:
            state.set(_KEY_BACKOFF_UNTIL, _iso(until))
        except Exception:
            logger.exception(
                "ImapPollTrigger %r: failed to store backoff", self.name,
            )

    def _clear_backoff(self) -> None:
        state = self.package_state
        if state is None:
            return
        try:
            state.delete(_KEY_BACKOFF_UNTIL)
        except Exception:
            pass

    # ── Watermark persistence (per-folder, UIDVALIDITY-aware) ─────────

    def _get_watermark(self, folder: str) -> tuple[int | None, int | None, int]:
        """Return ``(uid, uidvalidity, version)`` for ``folder``.

        ``version`` is the package_state row version for CAS; 0 when the
        row is absent (so a CAS with expected_version=0 inserts it).
        """
        state = self.package_state
        if state is None:
            return None, None, 0
        try:
            res = state.get_with_version(_watermark_key(folder))
        except Exception:
            logger.exception(
                "ImapPollTrigger %r: failed to read watermark for %r",
                self.name, folder,
            )
            return None, None, 0
        if res is None:
            return None, None, 0
        value, version = res
        if not isinstance(value, dict):
            return None, None, int(version)
        return (
            _coerce_int(value.get("uid")),
            _coerce_int(value.get("uidvalidity")),
            int(version),
        )

    def _set_watermark(
        self, folder: str, uid: int, uidvalidity: int, expected_version: int,
    ) -> bool:
        state = self.package_state
        if state is None:
            return False
        payload = {"uid": int(uid), "uidvalidity": int(uidvalidity)}
        try:
            return bool(state.cas(
                _watermark_key(folder), expected_version, payload,
            ))
        except Exception:
            logger.exception(
                "ImapPollTrigger %r: CAS on watermark for %r failed",
                self.name, folder,
            )
            return False

    # ── IMAP helpers ─────────────────────────────────────────────────

    def _connect(self, conn_info: tuple[str, int, str, str]) -> imaplib.IMAP4_SSL:
        host, port, username, password = conn_info
        conn = imaplib.IMAP4_SSL(host=host, port=port, timeout=_IMAP_TIMEOUT_SECONDS)
        conn.login(username, password)
        return conn

    @staticmethod
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

    @staticmethod
    def _select_uidvalidity(conn: imaplib.IMAP4_SSL, folder: str) -> int | None:
        """SELECT ``folder`` (read-only) and return its UIDVALIDITY."""
        typ, data = conn.select(folder, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"IMAP SELECT {folder!r} failed: {typ}")
        # imaplib exposes UIDVALIDITY via a separate untagged response.
        # NB: ``IMAP4.response(code)`` returns ``(code, data)`` — the first
        # element is the requested code string, NOT ``"OK"`` — so we must
        # NOT gate on ``typ == "OK"`` here (that always fails).  When the
        # untagged response is absent, ``data`` is ``[None]``.
        _typ, uv = conn.response("UIDVALIDITY")
        if uv and uv[0]:
            raw = uv[0]
            if isinstance(raw, bytes):
                raw = raw.decode("ascii", errors="replace")
            return _coerce_int(raw)
        return None

    @staticmethod
    def _search_uids_above(conn: imaplib.IMAP4_SSL, watermark: int) -> list[int]:
        """UID SEARCH for UIDs strictly greater than ``watermark``."""
        # ``UID SEARCH UID <lo>:*`` returns UIDs >= lo (server semantics
        # treat ``*`` as the highest UID).  We pass watermark+1 as lo and
        # then filter > watermark defensively (a server may include the
        # high UID even when lo > current-max).
        lo = watermark + 1
        typ, data = conn.uid("SEARCH", None, "UID", f"{lo}:*")
        if typ != "OK":
            raise RuntimeError(f"IMAP UID SEARCH failed: {typ}")
        raw = (data[0] or b"") if data else b""
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="replace")
        uids: list[int] = []
        for tok in raw.split():
            if not _UID_RE.match(tok):
                continue
            val = _coerce_int(tok)
            if val is not None and val > watermark:
                uids.append(val)
        return sorted(set(uids))

    @staticmethod
    def _search_max_uid(conn: imaplib.IMAP4_SSL) -> int:
        """Return the highest UID currently in the selected folder, or 0."""
        typ, data = conn.uid("SEARCH", None, "ALL")
        if typ != "OK":
            raise RuntimeError(f"IMAP UID SEARCH ALL failed: {typ}")
        raw = (data[0] or b"") if data else b""
        if isinstance(raw, bytes):
            raw = raw.decode("ascii", errors="replace")
        max_uid = 0
        for tok in raw.split():
            val = _coerce_int(tok)
            if val is not None and val > max_uid:
                max_uid = val
        return max_uid

    # ── Baselining + polling ─────────────────────────────────────────

    def _baseline_missing_folders(
        self, conn_info: tuple[str, int, str, str],
    ) -> None:
        """For any watched folder with no watermark yet, record its
        current max UID + UIDVALIDITY and emit nothing."""
        to_baseline = []
        for folder in self.folders:
            uid, _uv, _ver = self._get_watermark(folder)
            if uid is None:
                to_baseline.append(folder)
        if not to_baseline:
            return
        conn = None
        try:
            conn = self._connect(conn_info)
            for folder in to_baseline:
                uidvalidity = self._select_uidvalidity(conn, folder)
                max_uid = self._search_max_uid(conn)
                _uid, _uv, ver = self._get_watermark(folder)
                self._set_watermark(
                    folder, max_uid, uidvalidity or 0, ver,
                )
                logger.info(
                    "ImapPollTrigger %r: baselined folder %r at uid=%d "
                    "(uidvalidity=%s); emitting nothing",
                    self.name, folder, max_uid, uidvalidity,
                )
        finally:
            self._close_quietly(conn)

    def _do_poll(self) -> None:
        conn_info = self._resolve_connection()
        if conn_info is None:
            logger.debug(
                "ImapPollTrigger %r: EMAIL_IMAP_* not set; skipping",
                self.name,
            )
            return
        account = conn_info[2]  # IMAP_USERNAME
        conn = None
        try:
            conn = self._connect(conn_info)
        except (imaplib.IMAP4.error, OSError, socket.timeout) as exc:
            self._close_quietly(conn)
            self._set_backoff(_BACKOFF_SECONDS_ERROR)
            logger.warning(
                "ImapPollTrigger %r: IMAP connect/login failed (%s); "
                "backing off",
                self.name, exc,
            )
            return
        try:
            total_emitted = 0
            for folder in self.folders:
                total_emitted += self._poll_one_folder(conn, folder, account)
                if total_emitted >= _MAX_EMITS_PER_POLL:
                    break
            self._clear_backoff()
        except (imaplib.IMAP4.error, OSError, socket.timeout) as exc:
            self._set_backoff(_BACKOFF_SECONDS_ERROR)
            logger.warning(
                "ImapPollTrigger %r: IMAP error during poll (%s); backing off",
                self.name, exc,
            )
        except Exception:
            logger.exception("ImapPollTrigger %r: poll failed", self.name)
        finally:
            self._close_quietly(conn)

    def _poll_one_folder(
        self, conn: imaplib.IMAP4_SSL, folder: str, account: str,
    ) -> int:
        """Poll one folder; emit + advance watermark.  Returns emit count."""
        uidvalidity = self._select_uidvalidity(conn, folder)
        wm_uid, wm_uv, wm_ver = self._get_watermark(folder)

        # First run for this folder: baseline + emit nothing.
        if wm_uid is None:
            max_uid = self._search_max_uid(conn)
            self._set_watermark(folder, max_uid, uidvalidity or 0, wm_ver)
            logger.info(
                "ImapPollTrigger %r: first-run baseline folder %r at "
                "uid=%d (uidvalidity=%s)",
                self.name, folder, max_uid, uidvalidity,
            )
            return 0

        # UIDVALIDITY changed → the server renumbered the mailbox.  Old
        # UIDs are meaningless; re-baseline to current max and emit
        # nothing rather than re-fanning the whole mailbox.
        if uidvalidity is not None and wm_uv is not None and uidvalidity != wm_uv:
            max_uid = self._search_max_uid(conn)
            self._set_watermark(folder, max_uid, uidvalidity, wm_ver)
            logger.warning(
                "ImapPollTrigger %r: folder %r UIDVALIDITY changed "
                "(%s -> %s); reset watermark to uid=%d, emitting nothing",
                self.name, folder, wm_uv, uidvalidity, max_uid,
            )
            return 0

        new_uids = self._search_uids_above(conn, wm_uid)
        if not new_uids:
            logger.debug(
                "ImapPollTrigger %r: folder %r has no new UIDs above %d",
                self.name, folder, wm_uid,
            )
            return 0

        capped = new_uids[:_MAX_EMITS_PER_POLL]
        emitted = 0
        for uid in capped:
            payload = {
                "provider_message_id": str(uid),
                "folder": folder,
                "account": account,
            }
            self.emit(
                self.event_type,
                payload=payload,
                idempotency_key=f"imap-poll-{folder}-{uidvalidity}-{uid}",
            )
            emitted += 1

        # Advance the watermark to the highest UID we emitted, via CAS so
        # an overlapping poll cannot regress it.  If CAS loses the race,
        # another poll already advanced it; emits are idempotent on the
        # work-queue side, so we drop our update silently.
        new_high = capped[-1]
        if new_high > wm_uid:
            ok = self._set_watermark(
                folder, new_high, uidvalidity or (wm_uv or 0), wm_ver,
            )
            if not ok:
                logger.debug(
                    "ImapPollTrigger %r: watermark CAS lost race for %r "
                    "(expected_version=%d); leaving as-is",
                    self.name, folder, wm_ver,
                )
        logger.info(
            "ImapPollTrigger %r: emitted %d email.received event(s) from "
            "folder %r (new high uid=%d)",
            self.name, emitted, folder, new_high,
        )
        return emitted
