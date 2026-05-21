"""Data models for the carpenter-email review pipelines.

Every kind here is reserved at install time via the manifest's
``data_models:`` section and consumed by the JUDGE-dispatch
deserialiser.  The shapes are intentionally narrow: each field is
either a primitive (str / int / bool) or a PolicyLiteral subclass
that the JUDGE-dispatch wrapper validates against ``SecurityPolicies``
*before* the package's JUDGE handler runs (D24 I9).

Two flavours of extract are defined here:

* **Read extracts** (Phase 1, ``Email{SimpleText,MeetingInvite,
  OrderConfirmation}Extract``) — graduate header / body content from
  an untrusted Gmail message into trusted state.  Carry display-bound
  strings derived from email headers (see provenance warning below).
* **Write receipts** (Phase 1.5, ``Email{Send,Archive,MarkRead,Draft}
  Result``) — graduate the small structured outcome of an
  external-effect Gmail call (status literal + opaque ids + idempotency
  booleans).  No body or header content; the only attacker-controlled
  fields are opaque provider ids, which the JUDGE regex-bounds.

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

Header-derived string fields (provenance warning)
-------------------------------------------------

Several fields below — ``subject``, ``from_address`` (display name
component if present), ``location``, ``vendor``, ``order_id``,
``items``, and similar — are copied verbatim from the inbound
message's headers / body and may contain hostile content (prompt
injection, control characters, hidden Unicode, social-engineering
payloads).  The JUDGE handler bounds their length and bans control
chars, but it does NOT semantically sanitise them.

The trust contract is:

* These strings ARE displayed to a human (the user reads with
  appropriate skepticism — see ``kb/trust-warning.md``).
* They MUST NEVER be re-fed into LLM context outside of the explicit
  display path — e.g. don't take ``extract.subject`` and stuff it
  into a follow-up agent goal, system prompt, or KB article.  Doing
  so would re-open the prompt-injection path the templated
  REVIEWER + JUDGE pipeline closes.

If a future tool needs to act on these fields, route the action
through a fresh PLANNER/REVIEWER pair with its own JUDGE — never let
header-derived strings short-circuit into another agent's context.
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
    # Phase 1.5: write-side PLANNERs populate the recipient set that
    # was approved at the chat boundary.  The write-template REVIEWER
    # cross-checks the script's receipt against this list so a hostile
    # Gmail response cannot rewrite the to_addresses field on the
    # graduating extract.  Read templates ignore this field.
    staged_to_addresses: tuple[EmailPolicy, ...] = ()


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


# ---------------------------------------------------------------------------
# Phase 1.5 write-side receipts (REVIEWER -> JUDGE, pending until JUDGE approves)
# ---------------------------------------------------------------------------
#
# Each write-tool EXECUTOR script emits a structured JSON receipt to a raw
# Resource via ``files.write`` + ``resource.finalize``.  The REVIEWER reads
# that receipt + the briefing, then derives one of the four receipt
# dataclasses below.  The JUDGE handler validates the receipt before the
# Resource graduates to trusted context.
#
# Threat model contrasts with the read extracts:
#
# * Receipts carry NO email body or header content.  The only fields are
#   a status literal, the expected account email (EmailPolicy, validated
#   by the dispatch wrapper), opaque Gmail-issued ids, recipient lists
#   (EmailPolicy-typed, validated by the dispatch wrapper against the
#   allowlist), and booleans.
# * The provider-issued ``provider_message_id`` / ``draft_id`` are
#   attacker-influenceable (a compromised token could plausibly steer
#   them).  The JUDGE regex-bounds them to ``^[a-zA-Z0-9_-]{5,50}$``.
# * Status fields are ``Literal[...]`` typed; the JUDGE rejects any other
#   value rather than trusting the script's free-text status string.
#
# These dataclasses are NOT a substitute for the chat-boundary
# allowlist check (T1 / I9) or the chat-boundary user confirm (I9) — the
# write tool gates those before constructing the arc.  The
# receipts merely give the chat agent a typed, JUDGE-bound view of the
# operation's outcome so phrasing like "was_already_archived" is safe to
# repeat back to the user.


@dataclass(frozen=True)
class EmailSendResult:
    """Receipt from ``pkg_email_send_email``.

    Attributes:
        status: Always the literal ``"sent"`` on graduation; the JUDGE
            rejects any other value.  Errors short-circuit before the
            REVIEWER runs (the EXECUTOR script raises and the arc
            fails).
        expected_account_email: The mailbox the OAuth token belonged to
            at send time, copied from briefing.  Allowlist-validated.
        provider_message_id: Gmail's message id for the sent message.
            Opaque string; JUDGE bounds shape.
        to_addresses: Recipient set that was approved at the chat
            boundary and copied from briefing.staged_to_addresses.
            Each is EmailPolicy-typed so the dispatch wrapper rejects
            any address that has since been removed from the allowlist.
        schema_version: Bumped by package author when the dataclass
            shape changes.
    """

    status: str = "sent"
    expected_account_email: EmailPolicy = field(
        default_factory=lambda: EmailPolicy(""),
    )
    provider_message_id: str = ""
    to_addresses: tuple[EmailPolicy, ...] = ()
    schema_version: str = "1.0"


@dataclass(frozen=True)
class EmailArchiveResult:
    """Receipt from ``pkg_email_archive_email``.

    Attributes:
        status: Literal ``"archived"`` on graduation.
        expected_account_email: Mailbox the OAuth token belonged to.
        provider_message_id: Gmail message id that was archived.
        was_already_archived: True if the message had no ``INBOX``
            label at script time (the modify call was therefore a
            no-op).  Lets the chat agent phrase idempotency honestly.
        schema_version: Schema version.
    """

    status: str = "archived"
    expected_account_email: EmailPolicy = field(
        default_factory=lambda: EmailPolicy(""),
    )
    provider_message_id: str = ""
    was_already_archived: bool = False
    schema_version: str = "1.0"


@dataclass(frozen=True)
class EmailMarkReadResult:
    """Receipt from ``pkg_email_mark_read_email``.

    Attributes:
        status: Literal ``"marked_read"`` on graduation.
        expected_account_email: Mailbox the OAuth token belonged to.
        provider_message_id: Gmail message id that was marked read.
        was_already_read: True if the message had no ``UNREAD`` label
            at script time.
        schema_version: Schema version.
    """

    status: str = "marked_read"
    expected_account_email: EmailPolicy = field(
        default_factory=lambda: EmailPolicy(""),
    )
    provider_message_id: str = ""
    was_already_read: bool = False
    schema_version: str = "1.0"


@dataclass(frozen=True)
class EmailDraftResult:
    """Receipt from ``pkg_email_draft_email``.

    Attributes:
        status: Literal ``"drafted"`` on graduation.
        expected_account_email: Mailbox the OAuth token belonged to.
        provider_message_id: Gmail-assigned id of the staged message
            inside the draft.
        draft_id: Gmail-assigned id of the draft container itself.
        to_addresses: Recipients staged on the draft, copied from the
            briefing's ``staged_to_addresses``.  Allowlist-validated.
        schema_version: Schema version.
    """

    status: str = "drafted"
    expected_account_email: EmailPolicy = field(
        default_factory=lambda: EmailPolicy(""),
    )
    provider_message_id: str = ""
    draft_id: str = ""
    to_addresses: tuple[EmailPolicy, ...] = ()
    schema_version: str = "1.0"


# ---------------------------------------------------------------------------
# Phase 3a inbound-triage extract (REVIEWER -> JUDGE, pending until JUDGE approves)
# ---------------------------------------------------------------------------
#
# Emitted by the ``email_triage`` template that the ``gmail_poll`` trigger
# fans out for each newly-arrived message.  REVIEWER reads briefing +
# untrusted raw Gmail JSON; it derives EXACTLY one of these dataclasses
# (no body, no from-string, no subject content beyond a sanitized
# stripped form).  JUDGE bounds every field; the chat agent only ever
# sees the validated, typed extract.
#
# Trust contract (parallels Phase 1.5 v2 write-receipt model):
#
# * ``category`` is the Literal-typed classification.  REVIEWER picks
#   from a closed enumeration; JUDGE re-validates against the same
#   list so a smuggled value rejects.
# * ``from_address`` is an ``EmailPolicy`` — the dispatch wrapper
#   validates allowlist membership before our JUDGE handler runs.  The
#   JUDGE only confirms non-empty + control-free.
# * ``subject_clean`` is the ONLY free-form string field.  It is the
#   sanitized stripped form (control chars stripped; length bounded;
#   URLs forbidden in the prompt); JUDGE enforces the same constraints
#   plus a stricter URL ban.  No raw body, no raw subject, no headers
#   beyond from + sanitized-subject ever leave the JUDGE gate.
# * ``importance_flags`` is a bounded tuple of strings drawn from a
#   closed enum (``high_priority``, ``newsletter``, ``promotional``,
#   ``automated``, ``personal``, ``suspicious_keyword``).  JUDGE rejects
#   any unknown flag.

EMAIL_TRIAGE_CATEGORIES = (
    "personal",
    "transactional",
    "newsletter",
    "promotional",
    "automated",
    "unknown",
)

EMAIL_TRIAGE_FLAGS = (
    "high_priority",
    "newsletter",
    "promotional",
    "automated",
    "personal",
    "suspicious_keyword",
)


@dataclass(frozen=True)
class EmailTriageExtract:
    """Inbound-email triage extract emitted by the ``email_triage`` template.

    Phase 3a v1 surfaces this directly to chat-notify; later phases may
    fan a second pass into the per-kind read extracts (simple_text /
    meeting_invite / order_confirmation).  Field shapes are minimal by
    design — see the module docstring's trust-contract notes.

    Attributes:
        provider_message_id: Opaque Gmail message id (JUDGE regex-bounds).
        received_history_id: The Gmail ``historyId`` watermark that
            surfaced this message.  Opaque string; JUDGE bounds shape.
        category: One of :data:`EMAIL_TRIAGE_CATEGORIES`.  REVIEWER
            picks the best fit; JUDGE re-validates.
        from_address: Sender email, allowlist-validated by the dispatch
            wrapper before JUDGE runs.
        subject_clean: Sanitized subject form.  Control chars stripped,
            length-bounded (max 200 chars), URLs forbidden.  This is
            the ONLY free-form string field; it MUST be a stripped form
            the REVIEWER produced, not a verbatim header.
        importance_flags: Tuple of zero or more flags drawn from
            :data:`EMAIL_TRIAGE_FLAGS`.
        schema_version: Schema version (the JUDGE rejects mismatches).
    """

    provider_message_id: str = ""
    received_history_id: str = ""
    category: str = "unknown"
    from_address: EmailPolicy = field(
        default_factory=lambda: EmailPolicy(""),
    )
    subject_clean: str = ""
    importance_flags: tuple[str, ...] = ()
    schema_version: str = "1.0"
