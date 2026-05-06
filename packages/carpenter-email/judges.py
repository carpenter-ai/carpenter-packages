"""Deterministic JUDGE handlers for the carpenter-email read templates.

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

from dataclasses import dataclass, field
from typing import Any

from .data_models import (
    EmailMeetingInviteExtract,
    EmailOrderConfirmationExtract,
    EmailSimpleTextExtract,
)

# We import the extract dataclasses via the package-relative path so
# this module and ``data_models`` are loaded under the SAME namespaced
# slot in ``sys.modules`` (``_carpenter_pkg_.carpenter-email.data_models``).
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
    return JudgeVerdict.approve()
