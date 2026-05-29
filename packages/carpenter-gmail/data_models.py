"""Data models for the carpenter-gmail review pipelines.

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

Phase 3b adds an ``AttachmentMetadata`` dataclass and surfaces a
``attachments: tuple[AttachmentMetadata, ...]`` field on every read
extract plus the Phase 3a ``EmailTriageExtract``.  No bytes are
fetched (the bytes-yet question is deferred to Phase 3c); only
sender-claimed metadata — filename, MIME, size, attachment id,
inline disposition — is graduated.  See ``kb/attachments.md`` for
the trust contract.

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
# Phase 3b: attachment metadata (sub-component of read extracts)
# ---------------------------------------------------------------------------
#
# The REVIEWER walks the untrusted Gmail message payload's ``parts``
# tree and emits one ``AttachmentMetadata`` per part whose
# ``body.attachmentId`` is present.  The JUDGE handler regex- and
# bounds-checks each entry; on rejection, the entry does NOT graduate
# but the parent extract's flags/importance_flags is annotated with
# the literal ``"attachment_rejected"`` so the user can see that the
# package suppressed something rather than the chat agent silently
# under-reporting attachments.  See ``kb/attachments.md``.
#
# THE FIELDS ARE METADATA ONLY.  No bytes are fetched in Phase 3b;
# ``attachment_id`` is opaque and the chat agent must treat
# ``claimed_mime_type`` and ``size_bytes`` as advisory (sender-
# claimed, not verified).  ``filename_clean`` is DISPLAY-ONLY — it
# must never be used as a filesystem path component, URL segment,
# archive entry name, or shell argument.


@dataclass(frozen=True)
class AttachmentMetadata:
    """Sender-claimed metadata for one MIME part with an attachmentId.

    Attributes:
        filename_clean: The REVIEWER copies the part's ``filename``
            header verbatim; the JUDGE REJECTS (does NOT silently
            rewrite) entries that contain path separators, control
            characters, or bidirectional override codepoints.  Max
            128 chars.  Display-only.
        claimed_mime_type: The part's ``mimeType`` value (sender-
            claimed).  JUDGE bounds the shape (``a-zA-Z0-9._+-`` plus
            a single ``/``).  Never use to dispatch handlers.
        size_bytes: Gmail-reported decoded size (``body.size``).
            JUDGE bounds to ``[0, 100 MiB]``.  Treat as advisory.
        attachment_id: The opaque ``body.attachmentId`` Gmail returns.
            JUDGE bounds shape ``[a-zA-Z0-9_-]{5,512}``.  The chat
            agent must NOT parse it.  Phase 3b does not fetch the
            bytes; this id is reserved for the future bytes path.
        is_inline: ``True`` if any ``Content-Disposition`` header on
            the part starts (case-insensitive) with ``"inline"``;
            ``False`` otherwise (including the missing-header case,
            which is treated conservatively as a real attachment).
        schema_version: Always ``"1.0"`` in Phase 3b.  The JUDGE
            rejects mismatches.
    """

    filename_clean: str = ""
    claimed_mime_type: str = ""
    size_bytes: int = 0
    attachment_id: str = ""
    is_inline: bool = False
    schema_version: str = "1.0"


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
    # were present.  Free-form strings from a bounded list.  Phase 3b
    # may also append the literal ``"attachment_rejected"`` flag when
    # the JUDGE dropped one or more malformed AttachmentMetadata
    # entries from ``attachments``.
    flags: tuple[str, ...] = ()

    # Phase 3b: sender-claimed attachment metadata.  Empty by default;
    # the REVIEWER walks raw_email.payload.parts and populates this.
    # Each entry passes ``_check_attachment_metadata`` in the JUDGE;
    # rejected entries are dropped and the parent extract's ``flags``
    # gains ``"attachment_rejected"`` (D4 risk #8 mitigation).
    attachments: tuple["AttachmentMetadata", ...] = ()


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

    # Suspicious keywords found.  Phase 3b may append
    # ``"attachment_rejected"`` (see EmailSimpleTextExtract.flags note).
    flags: tuple[str, ...] = ()

    # Phase 3b: attachment metadata (typically the .ics calendar
    # invite plus any sender-included files).  Same trust contract as
    # the EmailSimpleTextExtract field — see AttachmentMetadata.
    attachments: tuple["AttachmentMetadata", ...] = ()


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

    # Phase 3b may append ``"attachment_rejected"`` here too.
    flags: tuple[str, ...] = ()

    # Phase 3b: attachment metadata (typically the PDF invoice
    # attached to a vendor receipt).  Same trust contract as the
    # EmailSimpleTextExtract field.
    attachments: tuple["AttachmentMetadata", ...] = ()


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
    """Receipt from ``pkg_gmail_send_email``.

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
    """Receipt from ``pkg_gmail_archive_email``.

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
    """Receipt from ``pkg_gmail_mark_read_email``.

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
    """Receipt from ``pkg_gmail_draft_email``.

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
    # Phase 3b: appended by the JUDGE when one or more
    # AttachmentMetadata entries were dropped from ``attachments``.
    "attachment_rejected",
    # Phase 3b: appended when the source message had more than 32
    # attachment parts and only the first 32 graduated.
    "too_many_attachments",
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
            :data:`EMAIL_TRIAGE_FLAGS`.  Phase 3b may additionally
            append the literal ``"attachment_rejected"`` flag when the
            JUDGE dropped one or more malformed AttachmentMetadata
            entries from ``attachments`` (D4 risk #8 mitigation), or
            ``"too_many_attachments"`` when the source message had
            more than 32 attachment parts.
        attachments: Phase 3b — sender-claimed metadata for each MIME
            part with an attachmentId.  See
            :class:`AttachmentMetadata`.  Empty by default; bounded to
            32 entries by the JUDGE.
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
    attachments: tuple["AttachmentMetadata", ...] = ()
    schema_version: str = "1.0"


# ---------------------------------------------------------------------------
# Phase 4: semantic resource index dataclasses
# ---------------------------------------------------------------------------
#
# Two new kinds power the email package's per-message semantic index:
#
# * ``EmailIndexFetchedBatch`` — the **per-tick metadata harvest**.
#   The EXECUTOR fetches a page of Gmail messages and writes one
#   receipt JSON; the REVIEWER copies each message's metadata fields
#   into a tuple of ``EmailIndexFetchedEntry`` rows; the JUDGE
#   (``judge_email_index_fetched_batch``) sanitises every field per
#   :func:`judges._sanitize_index_metadata`.  The trigger consumes
#   the graduated extract on its NEXT tick and calls
#   ``package_vectors.embed_and_upsert`` for each entry IN TRUSTED
#   CONTEXT.  The vector floats are therefore computed AFTER the
#   JUDGE has bounded every metadata field, and the embed call
#   never sees raw Gmail strings.  Trust invariant I3 is closed
#   twice over: metadata sanitisation prevents any hostile string
#   from landing in ``metadata_json`` at rest, and the post-JUDGE
#   embed step keeps the embedding service off the untrusted path.
#
# * ``EmailIndexBatchReceipt`` — the **per-tick audit trail** the
#   trigger writes (in trusted context, post-embed-and-upsert) so
#   the chat agent can phrase "indexed 4521 emails today, 12 errors".
#   JUDGE-bound counts and watermark monotonicity.
#
# Vector float values are never serialised into either dataclass.
# Per the package-internal invariant E1 noted in the trust audit, no
# trusted-context string carries vector data.


# Phase enum kept as a module-level constant so judges.py and the
# triggers can re-use it.  The literal values mirror the trigger names
# and arc template names ("1" / "2" / "incremental").
EMAIL_INDEX_PHASES = ("1", "2", "incremental")


# Hard upper bound on entries in a single batch.  Phase 1's nominal
# cap is 100; Phase 2 is 50; incremental is 25.  The JUDGE refuses
# anything above 100 because a hostile REVIEWER smuggling extra
# entries is exactly what this gate catches.
EMAIL_INDEX_MAX_BATCH = 100


@dataclass(frozen=True)
class EmailIndexFetchedEntry:
    """One message's worth of JUDGE-validated metadata.

    The REVIEWER copies these fields from the Gmail message payload
    after running them through ``_sanitize_index_metadata``; the
    JUDGE re-runs the same sanitiser and refuses to graduate any
    entry that fails.  Rejected entries are dropped from the batch
    (the message is skipped this tick; counted in ``skipped_count``
    on the parent batch).

    Attributes:
        provider_message_id: Opaque Gmail message id, bounded by
            ``^[a-zA-Z0-9_-]{5,50}$``.
        thread_id: Opaque Gmail thread id, same shape.
        from_address: Lowercase bare-address sender; rejected if it
            does not match the EmailPolicy address shape.
        from_display_clean: Sender display-name component with
            control chars, NUL, and bidi-override codepoints
            rejected.  Bounded <=128 chars.  May be empty.
        date_iso: ISO-8601 datetime parsed via
            ``datetime.fromisoformat`` with year in ``[1990, 2100]``.
        subject_raw: Sender-claimed Subject header.  Same
            sanitisation as ``from_display_clean``; bounded
            <=256 chars.  May be empty.
        gmail_snippet: Gmail's ~200-char preview field.  Same
            sanitisation; bounded <=256 chars.  May be empty.
        body_text_or_null: Phase-2 body harvest (empty for Phase 1
            and incremental).  Same sanitisation; bounded
            <=4000 chars.
        has_attachment: Boolean hint copied from Gmail's labelIds /
            payload walk.
        labels: Tuple of Gmail label strings (system labels like
            ``INBOX``/``IMPORTANT`` or user labels ``Label_<n>``).
            Bounded <=32 entries; each entry <=64 chars and matches
            the per-label regex.
        schema_version: ``"1.0"``.
    """

    provider_message_id: str = ""
    thread_id: str = ""
    from_address: str = ""
    from_display_clean: str = ""
    date_iso: str = ""
    subject_raw: str = ""
    gmail_snippet: str = ""
    body_text_or_null: str = ""
    has_attachment: bool = False
    labels: tuple[str, ...] = ()
    schema_version: str = "1.0"


@dataclass(frozen=True)
class EmailIndexFetchedBatch:
    """One indexer-tick's JUDGE-validated metadata entries.

    Produced by the REVIEWER (after reading the EXECUTOR's fetch
    receipt) and graduated by ``judge_email_index_fetched_batch``.
    The trigger reads this graduated extract on its next tick and
    embeds + upserts every entry in trusted context.

    Attributes:
        phase: One of :data:`EMAIL_INDEX_PHASES`.  Determines which
            embed-text composition the trigger uses.
        batch_id: REVIEWER-assigned opaque ``^[a-zA-Z0-9_-]{5,64}$``
            id, typically derived from the tick start time.  Log
            correlation only; no security surface.
        watermark_before: Opaque watermark snapshot the EXECUTOR
            read from ``package_state`` at fetch time.
        watermark_after: Opaque watermark snapshot the trigger will
            persist after embedding succeeds.  The JUDGE does NOT
            cross-check this against ``watermark_before`` for
            monotonicity — each phase has its own ordering rule
            and the trigger enforces it via CAS at write time.
        entries: Tuple of :class:`EmailIndexFetchedEntry`, length
            bounded by :data:`EMAIL_INDEX_MAX_BATCH`.
        fetched_count: How many messages the EXECUTOR fetched
            before any REVIEWER filtering.
        skipped_count: How many messages the REVIEWER rejected
            in-pass (e.g. obviously malformed envelope).  Bounded
            ``[0, fetched_count]``; JUDGE checks
            ``len(entries) + skipped_count == fetched_count``.
        error_kind: Optional structured-error tag.  Empty for a
            normal batch.  ``"model_identity_mismatch"`` is the
            D13 #2 special case — JUDGE allows graduating with
            empty ``entries`` so the trigger can pause indexing
            and the chat agent can surface a user-actionable alert.
        schema_version: ``"1.0"``.
    """

    phase: str = ""
    batch_id: str = ""
    watermark_before: str = ""
    watermark_after: str = ""
    entries: tuple[EmailIndexFetchedEntry, ...] = ()
    fetched_count: int = 0
    skipped_count: int = 0
    error_kind: str = ""
    schema_version: str = "1.0"


@dataclass(frozen=True)
class EmailIndexBatchReceipt:
    """Per-tick audit receipt the trigger writes post-embed-and-upsert.

    Constructed by the trigger in trusted context after every entry
    from the JUDGE-validated :class:`EmailIndexFetchedBatch` has
    been embedded + upserted (or skipped due to a per-entry embed
    error).  The chat agent reads this so it can phrase the
    indexing progress honestly.

    Attributes:
        phase: One of :data:`EMAIL_INDEX_PHASES`.
        batch_id: Mirrors the upstream batch's id.
        watermark_before: Mirrors the upstream batch's
            ``watermark_before``.
        watermark_after: Mirrors the upstream batch's
            ``watermark_after``.
        embedded_count: Vectors successfully upserted this tick.
            ``[0, EMAIL_INDEX_MAX_BATCH]``.
        error_count: Entries that failed embed/upsert (model-identity
            mismatch, transient embedding service error, etc.).
            ``[0, EMAIL_INDEX_MAX_BATCH]``.  ``embedded_count +
            error_count <= EMAIL_INDEX_MAX_BATCH``.
        sample_error_message: One-line, <=512 char, control-char-free
            summary of the most recent embed error.  Empty when
            ``error_count == 0``.  Display-only.
        schema_version: ``"1.0"``.
    """

    phase: str = ""
    batch_id: str = ""
    watermark_before: str = ""
    watermark_after: str = ""
    embedded_count: int = 0
    error_count: int = 0
    sample_error_message: str = ""
    schema_version: str = "1.0"
