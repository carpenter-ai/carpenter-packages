"""Deterministic JUDGE handlers for the carpenter-gmail read templates.

Each handler runs after the JUDGE-dispatch wrapper has already
validated the dataclass's PolicyLiteral fields against
``SecurityPolicies`` (D24 I9).  These handlers do only the
structural / cross-field checks the dataclass type system can't
express — control-character bans, length caps, schema-version
matches, expected-account consistency, and so on.

Trust contract (D24 I3):

* Each handler accepts exactly one positional argument: the
  deserialised dataclass.  No DB handle, no arc state, no raw
  bytes, no I/O.
* Each handler returns an object with ``.approved: bool`` and
  ``.reason: str``.  We use a small JudgeVerdict shim (importable
  via ``carpenter.security.judge.JudgeResult``); we duplicate the
  shape locally so the package has no static dependency on the
  platform's internal symbol path.
* Handler exceptions are caught by the wrapper and converted to
  a rejection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .data_models import (
    EMAIL_INDEX_MAX_BATCH,
    EMAIL_INDEX_PHASES,
    EMAIL_TRIAGE_CATEGORIES,
    EMAIL_TRIAGE_FLAGS,
    AttachmentMetadata,
    EmailArchiveResult,
    EmailDraftResult,
    EmailIndexBatchReceipt,
    EmailIndexFetchedBatch,
    EmailIndexFetchedEntry,
    EmailMarkReadResult,
    EmailMeetingInviteExtract,
    EmailOrderConfirmationExtract,
    EmailSendResult,
    EmailSimpleTextExtract,
    EmailTriageExtract,
)

# We import the extract dataclasses via the package-relative path so
# this module and ``data_models`` are loaded under the SAME namespaced
# slot in ``sys.modules`` (``_carpenter_pkg_.carpenter-gmail.data_models``).
# The platform's ``_import_package_module`` loader (which is what loads
# both this file and ``data_models``) ensures these class objects are
# identical to the ones the JUDGE-dispatch wrapper passes us, so
# ``isinstance`` is the correct, strongest check available.  Test code
# that wants ``isinstance``-compatible class identity must also go
# through ``_import_package_module`` (or its dependents like
# ``load_data_models`` / ``lookup_kind``) rather than constructing
# raw ``importlib.util.spec_from_file_location`` modules.


# ---------------------------------------------------------------------------
# Local JudgeVerdict shim
# ---------------------------------------------------------------------------
#
# The platform uses ``carpenter.security.judge.JudgeResult``; we duck-type
# match it (the dispatch wrapper reads ``.approved`` and ``.reason``)
# without importing platform internals — keeps the package's import
# graph clean.


@dataclass
class JudgeVerdict:
    """Duck-typed JUDGE result the platform's wrapper accepts."""

    approved: bool
    reason: str = ""
    checks: list = field(default_factory=list)

    @classmethod
    def approve(cls, reason: str = "") -> "JudgeVerdict":
        return cls(approved=True, reason=reason)

    @classmethod
    def reject(cls, reason: str) -> "JudgeVerdict":
        return cls(approved=False, reason=reason)


# ---------------------------------------------------------------------------
# Shared check helpers
# ---------------------------------------------------------------------------


# Control-char ban.  The REVIEWER's output is meant to be plain text
# the trusted parent will display; sneaking in NUL/BEL/CR/etc. could
# corrupt downstream tooling or render attacks.
_CONTROL_CHARS = frozenset(
    chr(c) for c in list(range(0, 9)) + [11, 12] + list(range(14, 32)) + [127]
)

_MAX_BODY_SUMMARY = 500
_MAX_SUBJECT = 300
_MAX_URLS = 16
_MAX_FLAGS = 16
_MAX_LOCATION = 200
_MAX_VENDOR = 200
_MAX_TOTAL = 64
_MAX_ORDER_ID = 128
_MAX_ITEM = 200
_MAX_ITEMS = 8
_SCHEMA_VERSION = "1.0"


def _has_control_chars(s: str) -> bool:
    """Return True if ``s`` contains any banned control character."""
    return any(c in _CONTROL_CHARS for c in s)


def _check_iso_datetime(s: str) -> bool:
    """Light-touch ISO-8601 / RFC-3339 well-formedness check.

    We do NOT parse the timezone exhaustively — we just sanity-check
    that the REVIEWER didn't return a control-char-laden free-form
    string.  Empty is treated as "not extracted" and is allowed
    (caller decides how to interpret).
    """
    if not s:
        return True
    if _has_control_chars(s):
        return False
    if len(s) > 64:
        return False
    # Has to start with a 4-digit year and contain a '-' for month.
    if len(s) < 10 or not s[:4].isdigit() or s[4] != "-":
        return False
    return True


def _check_envelope(extract: Any) -> str | None:
    """Run the checks every per-kind extract shares.

    Returns ``None`` on success or a rejection reason string.
    """
    if extract.schema_version != _SCHEMA_VERSION:
        return (
            f"unknown schema_version {extract.schema_version!r} "
            f"(expected {_SCHEMA_VERSION!r})"
        )
    if not extract.expected_account_email:
        return "expected_account_email is empty"
    if not extract.from_address:
        return "from_address is empty"
    if not extract.provider_message_id:
        return "provider_message_id is empty"
    if len(extract.subject) > _MAX_SUBJECT:
        return f"subject exceeds {_MAX_SUBJECT} chars"
    if _has_control_chars(extract.subject):
        return "subject contains control characters"
    if not _check_iso_datetime(extract.received_at):
        return f"received_at {extract.received_at!r} not ISO-8601-ish"
    # Recipient sanity: expected_account must appear in to or cc.
    expected = str(extract.expected_account_email).strip().lower()
    recipients = [str(a).strip().lower() for a in extract.to_addresses]
    recipients.extend(str(a).strip().lower() for a in extract.cc_addresses)
    if expected and expected not in recipients:
        return (
            "expected_account_email not present in to_addresses or "
            "cc_addresses; possible misrouted fetch"
        )
    return None


def _check_body_summary(s: str) -> str | None:
    if len(s) > _MAX_BODY_SUMMARY:
        return f"body_summary exceeds {_MAX_BODY_SUMMARY} chars"
    if _has_control_chars(s):
        return "body_summary contains control characters"
    return None


def _check_flags(flags: tuple[str, ...]) -> str | None:
    if len(flags) > _MAX_FLAGS:
        return f"too many flags ({len(flags)} > {_MAX_FLAGS})"
    for f in flags:
        if not isinstance(f, str):
            return f"flag {f!r} is not a string"
        if len(f) > 64:
            return f"flag {f!r} exceeds 64 chars"
        if _has_control_chars(f):
            return f"flag {f!r} contains control characters"
    return None


# ---------------------------------------------------------------------------
# Phase 3b: attachment-metadata checks
# ---------------------------------------------------------------------------
#
# Filename sanitization is REJECTION-ONLY.  The REVIEWER copies the
# Gmail-reported filename verbatim; the JUDGE either accepts it or
# refuses to graduate that AttachmentMetadata entry.  No silent
# rewrite — see D4 in /home/pi/notes/phase-3b-plan.md for why.

_ATTACHMENT_SCHEMA_VERSION = "1.0"
_MAX_ATTACHMENT_FILENAME = 128
_MAX_ATTACHMENT_MIME = 128
_MAX_ATTACHMENT_ID = 512
_MAX_ATTACHMENT_SIZE_BYTES = 100 * 1024 * 1024  # 100 MiB
_MAX_ATTACHMENTS = 32

# Filename: any Unicode codepoint EXCEPT C0 control range (0x00-0x1f),
# DEL (0x7f), forward slash, and backslash.  This is intentionally
# permissive on Unicode letters / punctuation (international filenames
# are legitimate); the BIDI-override codepoints are caught by the
# dedicated _BIDI_OVERRIDES set below.
_ATTACHMENT_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f/\\]+$")

# MIME type: ASCII-only, [type]/[subtype], each side bounded.  The
# inbound side starts with a non-special char to ban leading dots and
# leading slashes.
_ATTACHMENT_MIME_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,62}/[a-zA-Z0-9._+-]{1,62}$",
)

# Attachment id: Gmail's base64-url-safe-ish opaque token.  Loose
# 5..512 envelope.
_ATTACHMENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{5,512}$")

# Unicode bidirectional override codepoints.  These enable
# "invoice.exe" -> visually rendering as "invoice.txt" attacks; even
# though filename_clean is display-only, surfacing a misrepresented
# extension to the user is exactly the kind of confusion the JUDGE
# exists to prevent.  Reject any filename containing one.
_BIDI_OVERRIDES = frozenset(
    chr(c) for c in (
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # LRE/RLE/PDF/LRO/RLO
        0x2066, 0x2067, 0x2068, 0x2069,           # LRI/RLI/FSI/PDI
    )
)


def _check_attachment_metadata(am: Any) -> str | None:
    """Return ``None`` if ``am`` is a valid AttachmentMetadata, else a reason.

    The JUDGE-dispatch wrapper does NOT pre-validate nested
    dataclasses (AttachmentMetadata has zero PolicyLiteral fields),
    so this handler does ALL of the structural work.
    """
    if not isinstance(am, AttachmentMetadata):
        return f"attachment is not AttachmentMetadata: {type(am).__name__}"
    if am.schema_version != _ATTACHMENT_SCHEMA_VERSION:
        return (
            f"attachment schema_version {am.schema_version!r} "
            f"(expected {_ATTACHMENT_SCHEMA_VERSION!r})"
        )
    # Filename
    fname = am.filename_clean
    if not isinstance(fname, str):
        return f"filename_clean is not a string: {type(fname).__name__}"
    if not fname:
        return "filename_clean is empty"
    if len(fname) > _MAX_ATTACHMENT_FILENAME:
        return (
            f"filename_clean exceeds {_MAX_ATTACHMENT_FILENAME} chars "
            f"({len(fname)})"
        )
    if _has_control_chars(fname):
        return "filename_clean contains control characters"
    if not _ATTACHMENT_NAME_RE.match(fname):
        return (
            "filename_clean contains a path separator or other banned "
            "character"
        )
    if fname == "." or fname == "..":
        return f"filename_clean {fname!r} is a path-traversal sequence"
    for c in fname:
        if c in _BIDI_OVERRIDES:
            return (
                "filename_clean contains a bidirectional override "
                "codepoint"
            )
    # MIME
    mime = am.claimed_mime_type
    if not isinstance(mime, str):
        return (
            f"claimed_mime_type is not a string: {type(mime).__name__}"
        )
    if not mime:
        return "claimed_mime_type is empty"
    if len(mime) > _MAX_ATTACHMENT_MIME:
        return (
            f"claimed_mime_type exceeds {_MAX_ATTACHMENT_MIME} chars"
        )
    if not _ATTACHMENT_MIME_RE.match(mime):
        return (
            f"claimed_mime_type {mime!r} is not a valid type/subtype"
        )
    # Size — bool is a subclass of int, exclude explicitly.  Also exclude
    # float / str.
    size = am.size_bytes
    if isinstance(size, bool) or not isinstance(size, int):
        return f"size_bytes is not an int: {type(size).__name__}"
    if size < 0:
        return f"size_bytes is negative ({size})"
    if size > _MAX_ATTACHMENT_SIZE_BYTES:
        return (
            f"size_bytes {size} exceeds {_MAX_ATTACHMENT_SIZE_BYTES} "
            f"(100 MiB)"
        )
    # Attachment id
    aid = am.attachment_id
    if not isinstance(aid, str):
        return f"attachment_id is not a string: {type(aid).__name__}"
    if not aid:
        return "attachment_id is empty"
    if not _ATTACHMENT_ID_RE.match(aid):
        return (
            f"attachment_id {aid!r} does not match expected shape "
            f"^[a-zA-Z0-9_-]{{5,512}}$"
        )
    # is_inline
    if not isinstance(am.is_inline, bool):
        return (
            f"is_inline is not a bool: {type(am.is_inline).__name__}"
        )
    return None


def _check_attachments_list(attachments: Any) -> str | None:
    """Return ``None`` if ``attachments`` is a valid list, else a reason.

    Enforces tuple-ness, length cap, per-entry validation, and no
    duplicate ``attachment_id`` across entries.
    """
    if not isinstance(attachments, tuple):
        return (
            f"attachments is not a tuple: "
            f"{type(attachments).__name__}"
        )
    if len(attachments) > _MAX_ATTACHMENTS:
        return (
            f"attachments has {len(attachments)} entries "
            f"(max {_MAX_ATTACHMENTS})"
        )
    seen_ids: set[str] = set()
    for i, am in enumerate(attachments):
        err = _check_attachment_metadata(am)
        if err:
            return f"attachments[{i}]: {err}"
        if am.attachment_id in seen_ids:
            return (
                f"attachments[{i}]: duplicate attachment_id "
                f"{am.attachment_id!r}"
            )
        seen_ids.add(am.attachment_id)
    return None


# ---------------------------------------------------------------------------
# Per-kind handlers
# ---------------------------------------------------------------------------


def judge_simple_text(extract: Any) -> JudgeVerdict:
    """JUDGE for ``email_read_simple_text``.

    Approves only if envelope + body summary + URLs all pass the
    structural checks.  Policy-typed fields (from_address,
    to_addresses, cc_addresses, extracted_urls) are already
    validated by the dispatch wrapper before this runs.
    """
    # The JUDGE-dispatch wrapper already guarantees the dataclass
    # shape matches the template's ``extract_kind`` declaration, so
    # this check is a belt-and-braces type assertion, not the
    # load-bearing gate.
    if not isinstance(extract, EmailSimpleTextExtract):
        return JudgeVerdict.reject(
            f"expected EmailSimpleTextExtract, got {type(extract).__name__}",
        )
    err = _check_envelope(extract)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_body_summary(extract.body_summary)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_flags(extract.flags)
    if err:
        return JudgeVerdict.reject(err)
    if len(extract.extracted_urls) > _MAX_URLS:
        return JudgeVerdict.reject(
            f"too many URLs ({len(extract.extracted_urls)} > {_MAX_URLS})",
        )
    # Phase 3b: validate attachment metadata.
    err = _check_attachments_list(extract.attachments)
    if err:
        return JudgeVerdict.reject(err)
    return JudgeVerdict.approve()


def judge_meeting_invite(extract: Any) -> JudgeVerdict:
    """JUDGE for ``email_read_meeting_invite``.

    Adds checks for the meeting-specific fields: start/end timestamps
    are ISO-8601-ish, location is bounded and control-free, organizer
    is a valid policy email (already checked by dispatch wrapper).
    """
    if not isinstance(extract, EmailMeetingInviteExtract):
        return JudgeVerdict.reject(
            f"expected EmailMeetingInviteExtract, got "
            f"{type(extract).__name__}",
        )
    err = _check_envelope(extract)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_body_summary(extract.body_summary)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_flags(extract.flags)
    if err:
        return JudgeVerdict.reject(err)
    if not _check_iso_datetime(extract.start_at):
        return JudgeVerdict.reject(
            f"start_at {extract.start_at!r} not ISO-8601-ish",
        )
    if not _check_iso_datetime(extract.end_at):
        return JudgeVerdict.reject(
            f"end_at {extract.end_at!r} not ISO-8601-ish",
        )
    if len(extract.location) > _MAX_LOCATION:
        return JudgeVerdict.reject(
            f"location exceeds {_MAX_LOCATION} chars",
        )
    if _has_control_chars(extract.location):
        return JudgeVerdict.reject("location contains control characters")
    # Phase 3b: validate attachment metadata (typically the .ics part).
    err = _check_attachments_list(extract.attachments)
    if err:
        return JudgeVerdict.reject(err)
    return JudgeVerdict.approve()


def judge_order_confirmation(extract: Any) -> JudgeVerdict:
    """JUDGE for ``email_read_order_confirmation``.

    Adds checks for vendor / total / order_id / items: each is bounded
    and free of control chars; the items list is capped at
    ``_MAX_ITEMS``.
    """
    if not isinstance(extract, EmailOrderConfirmationExtract):
        return JudgeVerdict.reject(
            f"expected EmailOrderConfirmationExtract, got "
            f"{type(extract).__name__}",
        )
    err = _check_envelope(extract)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_body_summary(extract.body_summary)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_flags(extract.flags)
    if err:
        return JudgeVerdict.reject(err)
    for fld_name, val, cap in (
        ("vendor", extract.vendor, _MAX_VENDOR),
        ("total", extract.total, _MAX_TOTAL),
        ("order_id", extract.order_id, _MAX_ORDER_ID),
    ):
        if len(val) > cap:
            return JudgeVerdict.reject(f"{fld_name} exceeds {cap} chars")
        if _has_control_chars(val):
            return JudgeVerdict.reject(
                f"{fld_name} contains control characters",
            )
    if len(extract.items) > _MAX_ITEMS:
        return JudgeVerdict.reject(
            f"too many items ({len(extract.items)} > {_MAX_ITEMS})",
        )
    for item in extract.items:
        if not isinstance(item, str):
            return JudgeVerdict.reject(f"item {item!r} is not a string")
        if len(item) > _MAX_ITEM:
            return JudgeVerdict.reject(f"item exceeds {_MAX_ITEM} chars")
        if _has_control_chars(item):
            return JudgeVerdict.reject(
                f"item {item[:32]!r} contains control characters",
            )
    # Phase 3b: validate attachment metadata (typically a PDF invoice).
    err = _check_attachments_list(extract.attachments)
    if err:
        return JudgeVerdict.reject(err)
    return JudgeVerdict.approve()


# ---------------------------------------------------------------------------
# Phase 1.5 write-receipt handlers
# ---------------------------------------------------------------------------
#
# Each write-tool template produces one of the EmailXxxResult dataclasses
# from a small Gmail HTTP response.  The JUDGE handlers below validate
# the structural surface — the EmailPolicy fields are already validated
# by the dispatch wrapper before we run.


# Gmail-issued message and draft ids are opaque base64-url-like strings.
# We bound the alphabet and length tightly so a malicious response body
# cannot smuggle a payload through this field.
_PROVIDER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{5,50}$")

_MAX_RECIPIENTS = 32


def _check_provider_message_id(value: Any) -> str | None:
    """Return ``None`` if ``value`` is a valid Gmail provider id."""
    if not isinstance(value, str):
        return f"provider_message_id is not a string: {type(value).__name__}"
    if not _PROVIDER_ID_RE.match(value):
        return (
            f"provider_message_id {value!r} does not match expected "
            f"shape ^[a-zA-Z0-9_-]{{5,50}}$"
        )
    return None


def _check_draft_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return f"draft_id is not a string: {type(value).__name__}"
    if not _PROVIDER_ID_RE.match(value):
        return (
            f"draft_id {value!r} does not match expected shape "
            f"^[a-zA-Z0-9_-]{{5,50}}$"
        )
    return None


def _check_expected_account(value: Any) -> str | None:
    """Return ``None`` if ``value`` is a non-empty EmailPolicy literal.

    The dispatch wrapper has already validated the allowlist membership
    on ``EmailPolicy`` fields; here we just guard against the empty
    default (an unset expected_account would make the entire trust
    chain unenforceable).
    """
    s = str(value or "").strip()
    if not s:
        return "expected_account_email is empty"
    if _has_control_chars(s):
        return "expected_account_email contains control characters"
    return None


def _check_recipient_list(
    name: str, value: Any, *, allow_empty: bool,
) -> str | None:
    """Validate a ``tuple[EmailPolicy, ...]`` recipient field.

    The dispatch wrapper has already validated each entry against the
    email allowlist.  We additionally enforce: non-empty (where
    required), bounded length, every entry is a non-empty string with
    no control characters.
    """
    if not isinstance(value, tuple):
        return f"{name} is not a tuple: {type(value).__name__}"
    if not value:
        if allow_empty:
            return None
        return f"{name} is empty"
    if len(value) > _MAX_RECIPIENTS:
        return f"{name} has {len(value)} entries (max {_MAX_RECIPIENTS})"
    for entry in value:
        s = str(entry or "").strip()
        if not s:
            return f"{name} contains an empty entry"
        if _has_control_chars(s):
            return f"{name} entry {s[:32]!r} contains control characters"
    return None


def _check_write_schema_version(value: Any) -> str | None:
    if value != _SCHEMA_VERSION:
        return (
            f"unknown schema_version {value!r} "
            f"(expected {_SCHEMA_VERSION!r})"
        )
    return None


def judge_email_send(extract: Any) -> JudgeVerdict:
    """JUDGE for the ``email_write_send`` template.

    Approves only if:

    * the dataclass type matches (belt-and-braces; the dispatch wrapper
      enforces this too);
    * ``status`` is exactly ``"sent"`` (Literal-style validation done
      in Python — the field is a plain ``str`` on the dataclass so the
      JUDGE is the trust gate);
    * ``schema_version`` matches the package's current version;
    * ``expected_account_email`` is set and control-free;
    * ``provider_message_id`` matches the Gmail-id shape;
    * ``to_addresses`` is non-empty and bounded — the dispatch wrapper
      has already validated each address against the email allowlist.
    """
    if not isinstance(extract, EmailSendResult):
        return JudgeVerdict.reject(
            f"expected EmailSendResult, got {type(extract).__name__}",
        )
    err = _check_write_schema_version(extract.schema_version)
    if err:
        return JudgeVerdict.reject(err)
    if extract.status != "sent":
        return JudgeVerdict.reject(
            f"status {extract.status!r} is not the literal 'sent'",
        )
    err = _check_expected_account(extract.expected_account_email)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_provider_message_id(extract.provider_message_id)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_recipient_list(
        "to_addresses", extract.to_addresses, allow_empty=False,
    )
    if err:
        return JudgeVerdict.reject(err)
    return JudgeVerdict.approve()


# ---------------------------------------------------------------------------
# Phase 3a inbound-triage JUDGE
# ---------------------------------------------------------------------------
#
# The ``email_triage`` template's REVIEWER reads briefing + untrusted Gmail
# JSON and derives exactly one ``EmailTriageExtract``.  The JUDGE below
# enforces:
#
# * Dataclass type identity (belt-and-braces).
# * Schema version match.
# * ``category`` is in the closed enum ``EMAIL_TRIAGE_CATEGORIES``.
# * ``from_address`` is non-empty (dispatch wrapper already allowlist-
#   validated).
# * ``provider_message_id`` matches the opaque-id shape.
# * ``received_history_id`` is a bounded opaque string.
# * ``subject_clean`` is length-bounded, control-free, and contains no
#   URL substring (the REVIEWER must have stripped them; a hostile
#   REVIEWER smuggling URLs through this field is what this gate exists
#   to catch — see I3 in the trust-invariant audit).
# * ``importance_flags`` is a bounded tuple of strings drawn from the
#   closed enum ``EMAIL_TRIAGE_FLAGS``.

_MAX_TRIAGE_SUBJECT = 200
_MAX_TRIAGE_FLAGS = 8
_HISTORY_ID_RE = re.compile(r"^[0-9]{1,32}$")
# Cheap URL/scheme sniffer.  Matches the canonical http(s):// + bare
# ``www.`` prefix + ``://`` segment.  REVIEWER-emitted subject_clean
# is supposed to omit URLs entirely; we re-check here.
_URL_SUBSTR_RE = re.compile(
    r"(?i)\b(?:https?://|www\.[a-z0-9-]|ftp://|://)",
)


def judge_email_triage(extract: Any) -> JudgeVerdict:
    """JUDGE for ``email_triage``.

    The trust-graduation gate between an untrusted Gmail message
    fetched by the polling pipeline and the chat-notify path.  Approves
    only if every field passes the closed-enum / length / shape checks
    above.

    The REVIEWER's static prompt forbids copying subject / from /
    snippet verbatim into the extract; this handler re-checks the
    derivable parts.  Subject is length-bounded, control-free, and
    URL-free — three properties that compose to "won't smuggle a
    payload from an attacker-controlled Gmail message through chat".
    """
    if not isinstance(extract, EmailTriageExtract):
        return JudgeVerdict.reject(
            f"expected EmailTriageExtract, got {type(extract).__name__}",
        )
    err = _check_write_schema_version(extract.schema_version)
    if err:
        return JudgeVerdict.reject(err)
    if extract.category not in EMAIL_TRIAGE_CATEGORIES:
        return JudgeVerdict.reject(
            f"category {extract.category!r} is not in the closed enum "
            f"{sorted(EMAIL_TRIAGE_CATEGORIES)}",
        )
    err = _check_expected_account(extract.from_address)
    if err:
        # _check_expected_account is named for the write path; semantics
        # apply identically — non-empty + control-free.  Rewrite the
        # field name in the reason for log clarity.
        return JudgeVerdict.reject(
            err.replace("expected_account_email", "from_address"),
        )
    err = _check_provider_message_id(extract.provider_message_id)
    if err:
        return JudgeVerdict.reject(err)
    # received_history_id is an opaque numeric string (Gmail returns
    # decimal historyIds).  Bound shape so a hostile script cannot
    # smuggle a payload through it.
    hid = extract.received_history_id
    if not isinstance(hid, str) or not _HISTORY_ID_RE.match(hid):
        return JudgeVerdict.reject(
            f"received_history_id {hid!r} must match ^[0-9]{{1,32}}$",
        )
    subj = extract.subject_clean
    if not isinstance(subj, str):
        return JudgeVerdict.reject(
            f"subject_clean is not a string: {type(subj).__name__}",
        )
    if len(subj) > _MAX_TRIAGE_SUBJECT:
        return JudgeVerdict.reject(
            f"subject_clean exceeds {_MAX_TRIAGE_SUBJECT} chars "
            f"({len(subj)})",
        )
    if _has_control_chars(subj):
        return JudgeVerdict.reject(
            "subject_clean contains control characters",
        )
    if _URL_SUBSTR_RE.search(subj):
        return JudgeVerdict.reject(
            "subject_clean contains a URL-like substring; REVIEWER must "
            "strip URLs from the sanitised subject",
        )
    flags = extract.importance_flags
    if not isinstance(flags, tuple):
        return JudgeVerdict.reject(
            f"importance_flags is not a tuple: {type(flags).__name__}",
        )
    if len(flags) > _MAX_TRIAGE_FLAGS:
        return JudgeVerdict.reject(
            f"importance_flags has {len(flags)} entries "
            f"(max {_MAX_TRIAGE_FLAGS})",
        )
    seen_flags: set[str] = set()
    for fl in flags:
        if not isinstance(fl, str):
            return JudgeVerdict.reject(
                f"importance_flag {fl!r} is not a string",
            )
        if fl not in EMAIL_TRIAGE_FLAGS:
            return JudgeVerdict.reject(
                f"importance_flag {fl!r} is not in the closed enum "
                f"{sorted(EMAIL_TRIAGE_FLAGS)}",
            )
        if fl in seen_flags:
            return JudgeVerdict.reject(
                f"importance_flag {fl!r} is duplicated",
            )
        seen_flags.add(fl)
    # Phase 3b: validate attachment metadata.
    err = _check_attachments_list(extract.attachments)
    if err:
        return JudgeVerdict.reject(err)
    return JudgeVerdict.approve()


def judge_email_archive(extract: Any) -> JudgeVerdict:
    """JUDGE for the ``email_write_archive`` template.

    Approves when the receipt structurally describes a successful
    archive (or idempotent no-op archive) of one Gmail message.
    """
    if not isinstance(extract, EmailArchiveResult):
        return JudgeVerdict.reject(
            f"expected EmailArchiveResult, got {type(extract).__name__}",
        )
    err = _check_write_schema_version(extract.schema_version)
    if err:
        return JudgeVerdict.reject(err)
    if extract.status != "archived":
        return JudgeVerdict.reject(
            f"status {extract.status!r} is not the literal 'archived'",
        )
    err = _check_expected_account(extract.expected_account_email)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_provider_message_id(extract.provider_message_id)
    if err:
        return JudgeVerdict.reject(err)
    if not isinstance(extract.was_already_archived, bool):
        return JudgeVerdict.reject(
            f"was_already_archived is not a bool: "
            f"{type(extract.was_already_archived).__name__}",
        )
    return JudgeVerdict.approve()


def judge_email_mark_read(extract: Any) -> JudgeVerdict:
    """JUDGE for the ``email_write_mark_read`` template."""
    if not isinstance(extract, EmailMarkReadResult):
        return JudgeVerdict.reject(
            f"expected EmailMarkReadResult, got {type(extract).__name__}",
        )
    err = _check_write_schema_version(extract.schema_version)
    if err:
        return JudgeVerdict.reject(err)
    if extract.status != "marked_read":
        return JudgeVerdict.reject(
            f"status {extract.status!r} is not the literal 'marked_read'",
        )
    err = _check_expected_account(extract.expected_account_email)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_provider_message_id(extract.provider_message_id)
    if err:
        return JudgeVerdict.reject(err)
    if not isinstance(extract.was_already_read, bool):
        return JudgeVerdict.reject(
            f"was_already_read is not a bool: "
            f"{type(extract.was_already_read).__name__}",
        )
    return JudgeVerdict.approve()


def judge_email_draft(extract: Any) -> JudgeVerdict:
    """JUDGE for the ``email_write_draft`` template.

    Validates both the staged-message id and the draft container id,
    plus a non-empty recipient set that survived the dispatch
    wrapper's allowlist check.
    """
    if not isinstance(extract, EmailDraftResult):
        return JudgeVerdict.reject(
            f"expected EmailDraftResult, got {type(extract).__name__}",
        )
    err = _check_write_schema_version(extract.schema_version)
    if err:
        return JudgeVerdict.reject(err)
    if extract.status != "drafted":
        return JudgeVerdict.reject(
            f"status {extract.status!r} is not the literal 'drafted'",
        )
    err = _check_expected_account(extract.expected_account_email)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_provider_message_id(extract.provider_message_id)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_draft_id(extract.draft_id)
    if err:
        return JudgeVerdict.reject(err)
    err = _check_recipient_list(
        "to_addresses", extract.to_addresses, allow_empty=False,
    )
    if err:
        return JudgeVerdict.reject(err)
    return JudgeVerdict.approve()


# ---------------------------------------------------------------------------
# Phase 4: semantic resource index JUDGEs
# ---------------------------------------------------------------------------
#
# Two new handlers, both deterministic Python (no I/O, no DB handle):
#
# * ``judge_email_index_fetched_batch`` — the per-tick metadata harvest
#   that the trigger consumes on its NEXT tick.  Every field of every
#   entry passes through ``_sanitize_index_metadata``; rejected
#   entries are NOT mutated, the parent batch rejects.  This is
#   strict because trust invariant I3 hinges on no hostile string
#   smuggling into ``metadata_json`` at rest.
#
# * ``judge_email_index_batch`` — the per-tick audit receipt the
#   trigger itself writes post-embed.  Counts and sample error
#   bounds only; the receipt does not carry per-message data.
#
# The original D1 design from the Phase 4 plan was "no JUDGE on the
# upsert path".  The revised D12 design keeps that property (the
# embed_and_upsert call is in trusted context, post-JUDGE) AND adds
# a per-batch metadata JUDGE here, which is strictly stronger.

# Local Gmail-label regex.  System labels are uppercase ASCII (INBOX,
# IMPORTANT, CATEGORY_PROMOTIONS, ...); user labels match Gmail's
# internal ``Label_<n>`` form.  We reject anything else — labels
# enter ``metadata_json`` at rest and any wild Unicode is a
# potential display-spoof.
_INDEX_SYSTEM_LABEL_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_INDEX_USER_LABEL_RE = re.compile(r"^Label_[0-9]{1,32}$")
_INDEX_BATCH_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{5,64}$")
# Watermark strings stored in package_state are short opaque blobs
# (typically a Gmail historyId / internalDate / message id).  Bound
# the alphabet conservatively — these end up in trusted context via
# the receipt and we want them to be obviously inert.
_INDEX_WATERMARK_RE = re.compile(r"^[a-zA-Z0-9_:.-]{0,128}$")

_MAX_INDEX_FROM_DISPLAY = 128
_MAX_INDEX_SUBJECT = 256
_MAX_INDEX_SNIPPET = 256
_MAX_INDEX_BODY = 4000
_MAX_INDEX_LABELS = 32
_MAX_INDEX_LABEL_LEN = 64
_MAX_INDEX_SAMPLE_ERROR = 512
_MIN_INDEX_YEAR = 1990
_MAX_INDEX_YEAR = 2100

# Cheap inline email-address sanity check.  We deliberately avoid the
# full EmailPolicy import here because the JUDGE handler runs in a
# deterministic-Python context that should not touch the platform's
# allowlist (the index is package-internal — sender addresses don't
# need to be in the global allowlist to be indexable).  A bare-form
# regex is the right level of strictness: it rejects newlines /
# control chars / display-name leakage / multi-recipient strings, but
# allows international TLDs and plus-addressing.
_INDEX_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}$",
)

# Allowed structured error tags on the fetched batch.  Empty string
# means "normal batch".  Any other value rejects.
_INDEX_ERROR_KINDS = frozenset({
    "",
    "model_identity_mismatch",
    "history_expired",
})


def _check_index_string_field(
    name: str, value: Any, *, max_len: int, allow_empty: bool,
) -> str | None:
    """Common sanitisation for the free-text fields in an entry.

    Rejects (returns a reason) on:
      * non-string type
      * empty string when ``allow_empty=False``
      * length above ``max_len``
      * any control character (incl. NUL, CR, LF)
      * any bidirectional-override codepoint
        (see ``_BIDI_OVERRIDES`` at the top of this file)

    No mutation — this is the Phase 3b shape (reject, do not rewrite).
    """
    if not isinstance(value, str):
        return f"{name} is not a string: {type(value).__name__}"
    if not value:
        if allow_empty:
            return None
        return f"{name} is empty"
    if len(value) > max_len:
        return f"{name} exceeds {max_len} chars ({len(value)})"
    if _has_control_chars(value):
        return f"{name} contains control characters"
    for c in value:
        if c in _BIDI_OVERRIDES:
            return f"{name} contains a bidirectional override codepoint"
    return None


def _check_index_date_iso(value: Any) -> str | None:
    """Reject malformed ISO-8601 datetimes or out-of-range years.

    Allowed range ``[1990, 2100]``.  An entry with an obviously bogus
    date suggests either an exotic Gmail edge case (which we'd
    rather skip than store) or a tampered header.
    """
    if not isinstance(value, str):
        return f"date_iso is not a string: {type(value).__name__}"
    if not value:
        return "date_iso is empty"
    if len(value) > 64:
        return f"date_iso exceeds 64 chars ({len(value)})"
    # ``datetime.fromisoformat`` in 3.11+ accepts trailing ``Z`` for UTC
    # and full RFC-3339 offsets; in 3.10 ``Z`` would reject.  Normalise
    # so the JUDGE behaves identically across Pythons the platform
    # supports.
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        from datetime import datetime as _dt
        parsed = _dt.fromisoformat(s)
    except (TypeError, ValueError):
        return f"date_iso {value!r} is not parseable ISO-8601"
    if parsed.year < _MIN_INDEX_YEAR or parsed.year > _MAX_INDEX_YEAR:
        return (
            f"date_iso year {parsed.year} is outside "
            f"[{_MIN_INDEX_YEAR}, {_MAX_INDEX_YEAR}]"
        )
    return None


def _check_index_label(value: Any) -> str | None:
    """Reject any label not matching the system-or-user-label regex."""
    if not isinstance(value, str):
        return f"label is not a string: {type(value).__name__}"
    if not value:
        return "label is empty"
    if len(value) > _MAX_INDEX_LABEL_LEN:
        return (
            f"label exceeds {_MAX_INDEX_LABEL_LEN} chars ({len(value)})"
        )
    if (
        not _INDEX_SYSTEM_LABEL_RE.match(value)
        and not _INDEX_USER_LABEL_RE.match(value)
    ):
        return f"label {value!r} is neither a system nor user label form"
    return None


def _sanitize_index_metadata(entry: Any) -> str | None:
    """Return ``None`` if ``entry`` is a valid EmailIndexFetchedEntry.

    The shape mirrors :func:`_check_attachment_metadata`: reject-only,
    no mutation, no fall-through silent rewrites.  Rejected entries
    do not graduate; the parent batch tracks them via ``skipped_count``
    rather than storing partial bogus metadata.

    Order of checks is roughly cheapest-first so the common case
    (well-formed entry) runs quickly.
    """
    if not isinstance(entry, EmailIndexFetchedEntry):
        return (
            f"entry is not EmailIndexFetchedEntry: {type(entry).__name__}"
        )
    if entry.schema_version != _SCHEMA_VERSION:
        return (
            f"entry schema_version {entry.schema_version!r} "
            f"(expected {_SCHEMA_VERSION!r})"
        )
    # provider_message_id + thread_id — same shape as the existing
    # Gmail-provider-id regex.
    err = _check_provider_message_id(entry.provider_message_id)
    if err:
        return err
    if not isinstance(entry.thread_id, str):
        return f"thread_id is not a string: {type(entry.thread_id).__name__}"
    if not _PROVIDER_ID_RE.match(entry.thread_id):
        return (
            f"thread_id {entry.thread_id!r} does not match expected "
            f"shape ^[a-zA-Z0-9_-]{{5,50}}$"
        )
    # from_address — bare-form email, no display name, no commas.
    fa = entry.from_address
    if not isinstance(fa, str):
        return f"from_address is not a string: {type(fa).__name__}"
    if not fa:
        return "from_address is empty"
    if len(fa) > 254:
        return f"from_address exceeds 254 chars ({len(fa)})"
    if _has_control_chars(fa):
        return "from_address contains control characters"
    if fa != fa.lower():
        return "from_address is not lowercase"
    if not _INDEX_EMAIL_RE.match(fa):
        return f"from_address {fa!r} does not match expected email shape"
    # Display name (may be empty).
    err = _check_index_string_field(
        "from_display_clean", entry.from_display_clean,
        max_len=_MAX_INDEX_FROM_DISPLAY, allow_empty=True,
    )
    if err:
        return err
    # Date.
    err = _check_index_date_iso(entry.date_iso)
    if err:
        return err
    # Subject + snippet + body (each may be empty).
    err = _check_index_string_field(
        "subject_raw", entry.subject_raw,
        max_len=_MAX_INDEX_SUBJECT, allow_empty=True,
    )
    if err:
        return err
    err = _check_index_string_field(
        "gmail_snippet", entry.gmail_snippet,
        max_len=_MAX_INDEX_SNIPPET, allow_empty=True,
    )
    if err:
        return err
    err = _check_index_string_field(
        "body_text_or_null", entry.body_text_or_null,
        max_len=_MAX_INDEX_BODY, allow_empty=True,
    )
    if err:
        return err
    # has_attachment.  Bool is a subclass of int, so be explicit.
    if not isinstance(entry.has_attachment, bool):
        return (
            f"has_attachment is not a bool: "
            f"{type(entry.has_attachment).__name__}"
        )
    # Labels.
    if not isinstance(entry.labels, tuple):
        return f"labels is not a tuple: {type(entry.labels).__name__}"
    if len(entry.labels) > _MAX_INDEX_LABELS:
        return (
            f"labels has {len(entry.labels)} entries "
            f"(max {_MAX_INDEX_LABELS})"
        )
    seen_labels: set[str] = set()
    for i, lab in enumerate(entry.labels):
        err = _check_index_label(lab)
        if err:
            return f"labels[{i}]: {err}"
        if lab in seen_labels:
            return f"labels[{i}]: duplicate label {lab!r}"
        seen_labels.add(lab)
    return None


def judge_email_index_fetched_batch(extract: Any) -> JudgeVerdict:
    """JUDGE for ``email_index_phase1`` / ``_phase2`` / ``_incremental``.

    Approves only if EVERY entry passes :func:`_sanitize_index_metadata`
    AND the batch envelope (phase, batch_id, watermark shapes,
    fetched/skipped/error_kind invariants) is well-formed.  Rejects
    the whole batch on any per-entry failure — the trigger does not
    consume partial batches.
    """
    if not isinstance(extract, EmailIndexFetchedBatch):
        return JudgeVerdict.reject(
            f"expected EmailIndexFetchedBatch, got {type(extract).__name__}",
        )
    if extract.schema_version != _SCHEMA_VERSION:
        return JudgeVerdict.reject(
            f"schema_version {extract.schema_version!r} "
            f"(expected {_SCHEMA_VERSION!r})",
        )
    if extract.phase not in EMAIL_INDEX_PHASES:
        return JudgeVerdict.reject(
            f"phase {extract.phase!r} is not in the closed enum "
            f"{sorted(EMAIL_INDEX_PHASES)}",
        )
    if not isinstance(extract.batch_id, str):
        return JudgeVerdict.reject(
            f"batch_id is not a string: {type(extract.batch_id).__name__}",
        )
    if not _INDEX_BATCH_ID_RE.match(extract.batch_id):
        return JudgeVerdict.reject(
            f"batch_id {extract.batch_id!r} does not match expected "
            f"shape ^[a-zA-Z0-9_-]{{5,64}}$",
        )
    for fld_name, val in (
        ("watermark_before", extract.watermark_before),
        ("watermark_after", extract.watermark_after),
    ):
        if not isinstance(val, str):
            return JudgeVerdict.reject(
                f"{fld_name} is not a string: {type(val).__name__}",
            )
        if not _INDEX_WATERMARK_RE.match(val):
            return JudgeVerdict.reject(
                f"{fld_name} {val!r} does not match expected shape "
                f"^[a-zA-Z0-9_:.-]{{0,128}}$",
            )
    # error_kind: closed enum.
    if extract.error_kind not in _INDEX_ERROR_KINDS:
        return JudgeVerdict.reject(
            f"error_kind {extract.error_kind!r} is not in the closed enum "
            f"{sorted(_INDEX_ERROR_KINDS)}",
        )
    # Counts.
    for fld_name, val in (
        ("fetched_count", extract.fetched_count),
        ("skipped_count", extract.skipped_count),
    ):
        if isinstance(val, bool) or not isinstance(val, int):
            return JudgeVerdict.reject(
                f"{fld_name} is not an int: {type(val).__name__}",
            )
        if val < 0:
            return JudgeVerdict.reject(f"{fld_name} is negative ({val})")
        if val > EMAIL_INDEX_MAX_BATCH:
            return JudgeVerdict.reject(
                f"{fld_name} {val} exceeds cap {EMAIL_INDEX_MAX_BATCH}",
            )
    # Entries.
    if not isinstance(extract.entries, tuple):
        return JudgeVerdict.reject(
            f"entries is not a tuple: {type(extract.entries).__name__}",
        )
    if len(extract.entries) > EMAIL_INDEX_MAX_BATCH:
        return JudgeVerdict.reject(
            f"entries has {len(extract.entries)} entries "
            f"(max {EMAIL_INDEX_MAX_BATCH})",
        )
    # error_kind special case: if non-empty, the batch is a "pause
    # marker" and entries MUST be empty (the trigger will surface the
    # error rather than consume partial data).
    if extract.error_kind and extract.entries:
        return JudgeVerdict.reject(
            f"error_kind {extract.error_kind!r} requires empty entries, "
            f"got {len(extract.entries)}",
        )
    # Invariant: fetched = embedded + skipped (where embedded ~= len(entries)).
    if len(extract.entries) + extract.skipped_count != extract.fetched_count:
        return JudgeVerdict.reject(
            f"count invariant: len(entries)={len(extract.entries)} + "
            f"skipped_count={extract.skipped_count} != "
            f"fetched_count={extract.fetched_count}",
        )
    seen_ids: set[str] = set()
    for i, entry in enumerate(extract.entries):
        err = _sanitize_index_metadata(entry)
        if err:
            return JudgeVerdict.reject(f"entries[{i}]: {err}")
        if entry.provider_message_id in seen_ids:
            return JudgeVerdict.reject(
                f"entries[{i}]: duplicate provider_message_id "
                f"{entry.provider_message_id!r}",
            )
        seen_ids.add(entry.provider_message_id)
    return JudgeVerdict.approve()


def judge_email_index_batch(extract: Any) -> JudgeVerdict:
    """JUDGE for the per-tick :class:`EmailIndexBatchReceipt`.

    The receipt is constructed in trusted context by the trigger
    after embed-and-upsert has happened.  Approves only when counts
    are inside the absolute batch cap, watermark shapes are sane,
    and the sample error message (if any) is bounded and
    control-free.
    """
    if not isinstance(extract, EmailIndexBatchReceipt):
        return JudgeVerdict.reject(
            f"expected EmailIndexBatchReceipt, got {type(extract).__name__}",
        )
    if extract.schema_version != _SCHEMA_VERSION:
        return JudgeVerdict.reject(
            f"schema_version {extract.schema_version!r} "
            f"(expected {_SCHEMA_VERSION!r})",
        )
    if extract.phase not in EMAIL_INDEX_PHASES:
        return JudgeVerdict.reject(
            f"phase {extract.phase!r} is not in the closed enum "
            f"{sorted(EMAIL_INDEX_PHASES)}",
        )
    if not isinstance(extract.batch_id, str):
        return JudgeVerdict.reject(
            f"batch_id is not a string: {type(extract.batch_id).__name__}",
        )
    if not _INDEX_BATCH_ID_RE.match(extract.batch_id):
        return JudgeVerdict.reject(
            f"batch_id {extract.batch_id!r} does not match expected "
            f"shape ^[a-zA-Z0-9_-]{{5,64}}$",
        )
    for fld_name, val in (
        ("watermark_before", extract.watermark_before),
        ("watermark_after", extract.watermark_after),
    ):
        if not isinstance(val, str):
            return JudgeVerdict.reject(
                f"{fld_name} is not a string: {type(val).__name__}",
            )
        if not _INDEX_WATERMARK_RE.match(val):
            return JudgeVerdict.reject(
                f"{fld_name} {val!r} does not match expected shape "
                f"^[a-zA-Z0-9_:.-]{{0,128}}$",
            )
    # Counts.
    for fld_name, val in (
        ("embedded_count", extract.embedded_count),
        ("error_count", extract.error_count),
    ):
        if isinstance(val, bool) or not isinstance(val, int):
            return JudgeVerdict.reject(
                f"{fld_name} is not an int: {type(val).__name__}",
            )
        if val < 0:
            return JudgeVerdict.reject(f"{fld_name} is negative ({val})")
        if val > EMAIL_INDEX_MAX_BATCH:
            return JudgeVerdict.reject(
                f"{fld_name} {val} exceeds cap {EMAIL_INDEX_MAX_BATCH}",
            )
    if extract.embedded_count + extract.error_count > EMAIL_INDEX_MAX_BATCH:
        return JudgeVerdict.reject(
            f"embedded_count + error_count "
            f"({extract.embedded_count + extract.error_count}) exceeds "
            f"cap {EMAIL_INDEX_MAX_BATCH}",
        )
    # Sample error.
    sem = extract.sample_error_message
    if not isinstance(sem, str):
        return JudgeVerdict.reject(
            f"sample_error_message is not a string: {type(sem).__name__}",
        )
    if len(sem) > _MAX_INDEX_SAMPLE_ERROR:
        return JudgeVerdict.reject(
            f"sample_error_message exceeds {_MAX_INDEX_SAMPLE_ERROR} "
            f"chars ({len(sem)})",
        )
    if _has_control_chars(sem):
        return JudgeVerdict.reject(
            "sample_error_message contains control characters",
        )
    if extract.error_count == 0 and sem:
        return JudgeVerdict.reject(
            "sample_error_message must be empty when error_count is 0",
        )
    return JudgeVerdict.approve()
