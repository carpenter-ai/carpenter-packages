"""Data models for the carpenter-email Phase 1 review pipelines.

Every kind here is reserved at install time via the manifest's
``data_models:`` section and consumed by the JUDGE-dispatch
deserialiser.  The shapes are intentionally narrow: each field is
either a primitive (str / int / bool) or a PolicyLiteral subclass
that the JUDGE-dispatch wrapper validates against ``SecurityPolicies``
*before* the package's JUDGE handler runs (D24 I9).

Trust-model rationale
---------------------

The chat agent never sees the raw email body.  It sees one of these
dataclasses, and only after the JUDGE handler has approved it.  The
read pipeline shape is::

    PLANNER (trusted)
      -> writes EmailReviewBriefing (born-trusted)
    EXECUTOR (untrusted)
      -> fetches Gmail JSON via OAuth-bearer httpx call
      -> writes raw_email Resource (encrypted, untrusted)
    REVIEWER (constrained)
      -> reads briefing + raw_email
      -> writes Email{kind}Extract Resource (pending verdict)
    JUDGE (deterministic Python in this file's sibling judges.py)
      -> validates structural fields + policy literals
      -> flips verdict to approved / rejected
    Trusted parent reads the approved extract via read_resource

Each read template owns its own extract kind so a buggy or malicious
REVIEWER cannot upcast a "meeting invite" payload into the shape of
an "order confirmation" (which has different JUDGE-handler checks).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from carpenter_tools.policy.types import EmailPolicy, Url


# ---------------------------------------------------------------------------
# Briefing (PLANNER -> REVIEWER, born-trusted via derive_resource)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailReviewBriefing:
    """Trusted PLANNER -> templated REVIEWER handoff (D24 SD11).

    The PLANNER constructs this dataclass from the user's request,
    the global ``SecurityPolicies.email`` allowlist snapshot, and a
    static keyword list.  The REVIEWER reads it as input but cannot
    write to it.

    Attributes:
        expected_account_email: The mailbox we expect this fetch to
            be against.  REVIEWER and JUDGE both verify; mismatch =>
            reject.  Defends against a swapped-in refresh-token attack.
        senders_to_trust: Sender allowlist snapshot at PLANNER time.
            Frozen so the REVIEWER sees a stable view even if the
            global allowlist is mutated mid-fetch.
        suspicious_keywords: Static, package-controlled.  REVIEWER
            uses to populate ``flags``.
        extract_schema_version: Bumped by the package author when an
            extract dataclass changes shape; JUDGE rejects mismatched
            versions.
    """

    expected_account_email: EmailPolicy
    senders_to_trust: tuple[EmailPolicy, ...] = ()
    suspicious_keywords: tuple[str, ...] = (
        "wire transfer", "click here", "verify your account",
        "password expir", "ignore prior instructions", "act immediately",
    )
    extract_schema_version: str = "1.0"


# ---------------------------------------------------------------------------
# Per-kind extracts (REVIEWER -> JUDGE, pending until JUDGE approves)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EmailExtractBase:
    """Shared envelope fields for every per-kind extract.

    Not registered as a kind on its own; subclasses are.  Defining
    the common shape here keeps the JUDGE handlers parallel.
    """

    # Provider-agnostic message id.  Opaque string from the backend.
    provider_message_id: str = ""

    # The user's mailbox this was fetched from.  JUDGE checks this
    # matches the briefing's expected_account_email.
    expected_account_email: EmailPolicy = field(
        default_factory=lambda: EmailPolicy(""),
    )

    # Envelope.  PolicyLiteral fields are validated by the JUDGE
    # dispatch wrapper *before* the package handler runs.
    from_address: EmailPolicy = field(
        default_factory=lambda: EmailPolicy(""),
    )
    to_addresses: tuple[EmailPolicy, ...] = ()
    cc_addresses: tuple[EmailPolicy, ...] = ()
    subject: str = ""

    # ISO-8601 / RFC-3339-ish.  REVIEWER copies from Gmail's
    # internalDate.  JUDGE checks well-formedness.
    received_at: str = ""

    schema_version: str = "1.0"


@dataclass(frozen=True)
class EmailSimpleTextExtract(_EmailExtractBase):
    """Plain-text body summary for a single email message.

    The most common read shape.  REVIEWER produces a sanitised text
    summary (no URLs verbatim; URLs separately listed in
    ``extracted_urls``); JUDGE bans control characters and length
    overruns and ensures every URL is allowlisted.
    """

    # Sanitised, plain-text-only summary.  Max 500 chars.  URLs have
    # been replaced with ``[link omitted]``.
    body_summary: str = ""

    # Literal URLs the REVIEWER observed, deduplicated, max 16.
    # PolicyLiteral validation catches non-allowlisted URLs at
    # construction time.
    extracted_urls: tuple[Url, ...] = ()

    # Subset of briefing.suspicious_keywords the REVIEWER thinks
    # were present.  Free-form strings from a bounded list.
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmailMeetingInviteExtract(_EmailExtractBase):
    """Meeting / calendar invite extract.

    REVIEWER pulls structured event metadata; the trusted parent
    can decide whether to surface a Calendar tool flow.
    """

    # ISO-8601 start / end times.  Empty string means REVIEWER
    # could not extract them; JUDGE rejects malformed values.
    start_at: str = ""
    end_at: str = ""

    # Free-form location string (address or video-call link prefix).
    # JUDGE bans control characters and caps at 200 chars.
    location: str = ""

    # Organizer email (validated against the platform allowlist).
    organizer: EmailPolicy = field(
        default_factory=lambda: EmailPolicy(""),
    )

    # The body summary, same constraints as SimpleText.
    body_summary: str = ""

    # Suspicious keywords found.
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmailOrderConfirmationExtract(_EmailExtractBase):
    """Order / receipt confirmation extract.

    REVIEWER pulls vendor + amount metadata; trusted parent can
    surface a list to the user without ever reading the original
    HTML.
    """

    # Vendor name (free-form, JUDGE caps length and bans control chars).
    vendor: str = ""

    # Order total as a free-form string ("$42.99", "EUR 12,50").
    # We don't try to parse currency on the package side; the
    # JUDGE caps length and bans control chars.
    total: str = ""

    # Order-id-ish identifier from the vendor.
    order_id: str = ""

    # Up to 8 line-item descriptions (REVIEWER summarises;
    # JUDGE caps each item's length).
    items: tuple[str, ...] = ()

    body_summary: str = ""

    flags: tuple[str, ...] = ()
