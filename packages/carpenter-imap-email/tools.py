"""Chat tools for the carpenter-imap-email package.

These are the chat-boundary tools the chat agent calls.  Each is a
``@chat_tool``-decorated function.  Read-side and write-side tools both
fan out arc batches into the package's PLANNER -> EXECUTOR -> REVIEWER
-> JUDGE pipeline so every U->T graduation passes through a
deterministic JUDGE handler — identical trust shape to carpenter-gmail.

The ONLY backend difference from carpenter-gmail is how the EXECUTOR
reaches the network:

* Gmail's EXECUTOR reads an OAuth bearer from ``os.environ`` and calls
  ``web.get`` against a hardcoded ``gmail.googleapis.com`` URL.
* This package's EXECUTOR scripts are CRED-FREE and HOST-FREE — they
  dispatch the package's TRUSTED capability verbs (``imap.fetch`` /
  ``imap.search`` / ``imap.store`` / ``smtp.send``).  The trusted handler
  in ``handlers/imap_smtp.py`` supplies host + port + credentials from
  the operator-confirmed grant.

Everything else — the briefing builder, the REVIEWER prompts, the typed
extract dataclasses, the deterministic JUDGE handlers, the
arc-tree wiring — is composed verbatim from the shared
carpenter-email-core layer (``arc_builders.py``, ``data_models.py``,
``judges.py``, ``templates/``).
"""

from __future__ import annotations

import json
import logging

from carpenter.chat_tool_loader import chat_tool

# Backend-agnostic arc-tree builders + extract-kind maps + the RFC-822
# raw-message helper, composed in from the carpenter-email-core layer
# (see arc_builders.py).  This package binds its own pre-verified scripts
# and source prefix into these via the thin wrappers below; the builders
# themselves name no backend.
from .arc_builders import (
    EXTRACT_KIND_BY_TEMPLATE as _EXTRACT_KIND_BY_TEMPLATE,  # noqa: F401
    _WRITE_EXTRACT_KIND_BY_TEMPLATE,  # noqa: F401
    _build_raw_message,
    _create_read_arc_tree as _create_read_arc_tree_base,
    _create_triage_arc_tree as _create_triage_arc_tree_base,
    _create_write_arc_tree as _create_write_arc_tree_base,
)


logger = logging.getLogger(__name__)


# Credential env_key_prefix the operator supplies at install (matches
# the manifest's kind:env credential_requirement and the capability
# grants' credential_ref).  Resolved PLATFORM-SIDE by the handlers via
# ctx.secret(...) — the chat tools only read the username for the
# expected-account fail-closed check.
_ENV_KEY_PREFIX = "IMAP_EMAIL"

# Audit ``source_descriptor`` prefix for untrusted raw-fetch Resources
# created by this backend.  Passed into the shared arc builders so the
# on-disk descriptors read ``imap:...`` / ``imap-write:...``.
_IMAP_SOURCE_PREFIX = "imap"

# Default mailbox the read/search/store paths operate on.
_DEFAULT_MAILBOX = "INBOX"
_DRAFTS_MAILBOX = "Drafts"


# ---------------------------------------------------------------------------
# Helpers (no @chat_tool — package-internal)
# ---------------------------------------------------------------------------


def _resolve_expected_account() -> str:
    """Return the mailbox address Carpenter expects to be acting on.

    For the IMAP backend the configured account is the IMAP username the
    operator supplied at install (``IMAP_EMAIL_IMAP_USERNAME``), falling
    back to the platform operator email.  Returns empty string if
    neither is set; callers MUST treat empty as a fail-closed condition
    (the T1 envelope check is unenforceable without an expected account).

    Resolved PLATFORM-SIDE from the loaded config — the chat tool runs
    in trusted context, so reading the configured account here is safe.
    The untrusted EXECUTOR never sees it; only the typed briefing /
    extract carries ``expected_account_email`` through the JUDGE gate.
    """
    from carpenter import config

    return (
        config.CONFIG.get(f"{_ENV_KEY_PREFIX}_IMAP_USERNAME")
        or config.CONFIG.get("operator_email")
        or ""
    ).strip().lower()


_EXPECTED_ACCOUNT_NOT_CONFIGURED_ERROR = (
    "expected_account is not configured (neither IMAP_EMAIL_IMAP_USERNAME "
    "nor operator_email is set).  Supply the IMAP_EMAIL_* credentials at "
    "install — without an expected account the T1 envelope "
    "recipient-mismatch check cannot be enforced and read paths fail "
    "closed."
)


def _create_read_arc_tree(
    *,
    template_name: str,
    provider_message_id: str,
    expected_account_email: str,
    conversation_id: int | None,
) -> dict:
    """IMAP read arc tree: bind the IMAP fetch script + source prefix
    into the shared, backend-agnostic builder in ``arc_builders``."""
    from .scripts import IMAP_FETCH_SCRIPT

    result = _create_read_arc_tree_base(
        template_name=template_name,
        provider_message_id=provider_message_id,
        expected_account_email=expected_account_email,
        conversation_id=conversation_id,
        fetch_script=IMAP_FETCH_SCRIPT,
        raw_source_prefix=_IMAP_SOURCE_PREFIX,
    )
    # The IMAP fetch script also reads ``mailbox`` from EXECUTOR arc
    # state; seed it on the executor child the builder created.
    _seed_mailbox_on_executor(result, _DEFAULT_MAILBOX)
    return result


def _create_triage_arc_tree(
    *,
    provider_message_id: str,
    received_history_id: str,
    expected_account_email: str,
) -> dict:
    """IMAP triage arc tree (DEFERRED feature; wrapper kept for the
    composed triage_inbound handler's relative import).

    Binds the IMAP fetch script + source prefix into the shared builder.
    The MVP manifest does not declare the email.received subscription,
    so this is never invoked in v0.1.0 — but keeping the wrapper means
    the composed ``handlers/triage_inbound.py`` import resolves cleanly
    when inbound polling ships in v0.2.0.
    """
    from .scripts import IMAP_FETCH_SCRIPT

    result = _create_triage_arc_tree_base(
        provider_message_id=provider_message_id,
        received_history_id=received_history_id,
        expected_account_email=expected_account_email,
        fetch_script=IMAP_FETCH_SCRIPT,
        raw_source_prefix=_IMAP_SOURCE_PREFIX,
    )
    _seed_mailbox_on_executor(result, _DEFAULT_MAILBOX)
    return result


def _create_write_arc_tree(
    *,
    template_name: str,
    arc_name: str,
    arc_goal: str,
    script: str,
    state_seed: dict,
    expected_account_email: str,
    staged_to_addresses: tuple[str, ...],
    conversation_id: int | None,
) -> dict:
    """IMAP write arc tree: delegate to the shared builder in
    ``arc_builders`` with the IMAP source prefix.  ``script`` is the
    pre-verified write script the caller resolved from ``scripts.py``."""
    return _create_write_arc_tree_base(
        template_name=template_name,
        arc_name=arc_name,
        arc_goal=arc_goal,
        script=script,
        state_seed=state_seed,
        expected_account_email=expected_account_email,
        staged_to_addresses=staged_to_addresses,
        conversation_id=conversation_id,
        raw_source_prefix=_IMAP_SOURCE_PREFIX,
    )


def _seed_mailbox_on_executor(result: dict, mailbox: str) -> None:
    """Best-effort: seed ``mailbox`` on the EXECUTOR child of a freshly
    built read/triage arc tree so the IMAP fetch script can select it.

    The shared builder pre-seeds provider_message_id / raw_resource_*
    on the executor but knows nothing about IMAP mailboxes; we add the
    backend-specific key here.  Failures are non-fatal (the handler
    defaults to INBOX).
    """
    if not isinstance(result, dict) or "arc_id" not in result:
        return
    try:
        from carpenter.core.workflows._arc_state import set_arc_state
        from carpenter.db import db_connection
        parent_id = result["arc_id"]
        with db_connection() as db:
            row = db.execute(
                "SELECT id FROM arcs WHERE parent_id = ? "
                "AND agent_type = 'EXECUTOR' ORDER BY id LIMIT 1",
                (parent_id,),
            ).fetchone()
        if row is not None:
            set_arc_state(row["id"], "mailbox", mailbox)
    except Exception:  # noqa: BLE001 — non-fatal; handler defaults to INBOX
        logger.debug("could not seed mailbox on executor arc", exc_info=True)


def _create_search_executor(
    *,
    query: str,
    max_results: int,
    conversation_id: int | None,
) -> dict:
    """Run a single untrusted EXECUTOR that dispatches imap.search and
    writes the matching-UID JSON to a raw Resource.  Mirrors
    carpenter-gmail's ``_create_search_executor`` but uses the IMAP
    capability verb (cred-free / host-free) instead of a Gmail web.get.
    """
    from carpenter.core.arcs import manager as _am
    from carpenter.core.engine import work_queue as _wq
    from carpenter.core.resources import (
        create_resource as _create_resource,
        link_arc_resource as _link_arc_resource,
        resource_storage_path as _resource_storage_path,
    )
    from carpenter.core.workflows._arc_state import set_arc_state
    from carpenter.db import db_transaction as _db_transaction
    from carpenter.tool_backends import arc as arc_backend

    from .scripts import IMAP_SEARCH_SCRIPT

    parent_id = _am.create_arc(
        name=f"Email search: {query[:60]}",
        goal=(
            "Search the mailbox via the imap.search capability verb, "
            "then fan out N email_read_simple_text child arcs (one per "
            "matching UID).  Read the executor's id-list Resource (path "
            "in arc state under 'id_list_resource_path') and create "
            "child arc trees via the package's _create_read_arc_tree "
            "helper."
        ),
        agent_type="PLANNER",
    )
    if conversation_id:
        from carpenter.agent import conversation as _conv
        _conv.link_arc_to_conversation(conversation_id, parent_id)
    _am.update_status(parent_id, "active")

    batch_result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": f"Search mailbox: {query[:40]}",
                "goal": (
                    "Submit this EXACT code via submit_code:\n"
                    "```python\n" + IMAP_SEARCH_SCRIPT + "```\n"
                    "Inputs are pre-seeded in arc state."
                ),
                "parent_id": parent_id,
                "integrity_level": "untrusted",
                "output_type": "json",
                "agent_type": "EXECUTOR",
                "step_order": 0,
            },
        ],
    })
    if "error" in batch_result:
        try:
            _am.update_status(parent_id, "failed")
        except Exception:  # noqa: BLE001
            pass
        return {"error": batch_result["error"]}

    executor_arc_id = batch_result["arc_ids"][0]

    raw_id = _create_resource(
        content_type="json",
        file_path=None,
        produced_by_arc_id=executor_arc_id,
        source_descriptor=f"imap.search:{query}",
    )
    raw_path = _resource_storage_path(raw_id, "blob")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with _db_transaction() as _db:
        _db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (str(raw_path), raw_id),
        )
    _link_arc_resource(
        arc_id=executor_arc_id, resource_id=raw_id, role="output",
    )

    set_arc_state(executor_arc_id, "search_query", query)
    set_arc_state(executor_arc_id, "mailbox", _DEFAULT_MAILBOX)
    set_arc_state(executor_arc_id, "max_results", max_results)
    set_arc_state(executor_arc_id, "id_list_path", str(raw_path))
    set_arc_state(executor_arc_id, "raw_resource_id", raw_id)
    set_arc_state(parent_id, "_primary_resource_id", raw_id)
    set_arc_state(parent_id, "search_query", query)

    if conversation_id:
        from carpenter.agent import conversation as _conv
        _conv.link_arc_to_conversation(conversation_id, executor_arc_id)

    _wq.enqueue(
        "arc.dispatch",
        {"arc_id": executor_arc_id},
        idempotency_key=f"arc_dispatch:{executor_arc_id}",
    )
    return {"arc_id": parent_id}


# ---------------------------------------------------------------------------
# Read-side: list / search / read
# ---------------------------------------------------------------------------


@chat_tool(
    description=(
        "Search the mailbox via IMAP for messages matching a free-text "
        "query.  Runs a single untrusted EXECUTOR that dispatches the "
        "imap.search capability verb and writes the matching UIDs to a "
        "Resource; the chat agent fans out per-message read pipelines on "
        "the returned UIDs.  The chat agent never sees raw email bodies "
        "in this step."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-form search string, matched against message "
                    "text.  Empty string searches all messages."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of UIDs.  Capped at 25.",
                "default": 10,
                "minimum": 1,
                "maximum": 25,
            },
        },
        "required": ["query"],
    },
    capabilities=["arc_create"],
)
def pkg_imap_search_emails(tool_input, **kwargs):
    """Search the mailbox via the imap.search capability verb."""
    query = (tool_input.get("query") or "").strip()
    max_results = int(tool_input.get("max_results") or 10)
    if max_results < 1 or max_results > 25:
        return json.dumps({"error": "max_results must be between 1 and 25"})
    if not _resolve_expected_account():
        return json.dumps({"error": _EXPECTED_ACCOUNT_NOT_CONFIGURED_ERROR})

    result = _create_search_executor(
        query=query,
        max_results=max_results,
        conversation_id=kwargs.get("conversation_id"),
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps({
        "arc_id": result["arc_id"],
        "note": (
            "Search executor running.  When it completes, read "
            "individual UIDs with pkg_imap_read_email."
        ),
    })


@chat_tool(
    description=(
        "Fetch recent inbox UIDs and run email_read_simple_text on each. "
        "Returns the parent arc id; results arrive via the standard "
        "arc-completion notify path."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "max_results": {
                "type": "integer",
                "default": 10,
                "minimum": 1,
                "maximum": 25,
            },
        },
        "required": [],
    },
    capabilities=["arc_create"],
)
def pkg_imap_list_inbox(tool_input, **kwargs):
    """Equivalent to pkg_imap_search_emails(query='')."""
    n = int(tool_input.get("max_results") or 10)
    return pkg_imap_search_emails({"query": "", "max_results": n}, **kwargs)


@chat_tool(
    description=(
        "Read one specific message by IMAP UID.  Creates a single "
        "email read arc tree (PLANNER -> EXECUTOR -> REVIEWER -> JUDGE). "
        "Returns the parent arc id; the JUDGE-approved extract Resource "
        "arrives via arc-completion notify.  The chat agent never sees "
        "the raw email body."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider_message_id": {
                "type": "string",
                "description": "IMAP UID (decimal string).",
            },
            "kind": {
                "type": "string",
                "enum": [
                    "simple_text", "meeting_invite", "order_confirmation",
                ],
                "default": "simple_text",
                "description": "Which read template to use.",
            },
        },
        "required": ["provider_message_id"],
    },
    capabilities=["arc_create"],
)
def pkg_imap_read_email(tool_input, **kwargs):
    """Run one read template on one UID."""
    mid = (tool_input.get("provider_message_id") or "").strip()
    if not mid:
        return json.dumps({"error": "provider_message_id (UID) is required"})
    kind = (tool_input.get("kind") or "simple_text").strip()
    template_name = {
        "simple_text": "email_read_simple_text",
        "meeting_invite": "email_read_meeting_invite",
        "order_confirmation": "email_read_order_confirmation",
    }.get(kind)
    if not template_name:
        return json.dumps({"error": f"unknown kind {kind!r}"})
    expected = _resolve_expected_account()
    if not expected:
        return json.dumps({"error": _EXPECTED_ACCOUNT_NOT_CONFIGURED_ERROR})
    result = _create_read_arc_tree(
        template_name=template_name,
        provider_message_id=mid,
        expected_account_email=expected,
        conversation_id=kwargs.get("conversation_id"),
    )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Send-side
# ---------------------------------------------------------------------------


def _send_via_smtp(*, to_list, subject, body, in_reply_to, conversation_id):
    """Shared body for send + reply: validate recipients, build the
    RFC-822 message, and spin up the write arc tree bound to the SMTP
    send script."""
    from carpenter.security import get_policies

    from .scripts import SMTP_SEND_SCRIPT

    if not isinstance(to_list, list) or not to_list:
        return {"error": "to must be a non-empty list"}
    if not subject:
        return {"error": "subject is required"}
    if not body:
        return {"error": "body is required"}

    # Defence in depth: validate every recipient against the global
    # allowlist before building the arc (belt-and-braces with the
    # EmailPolicy literal validation in the JUDGE-dispatch wrapper).
    pol = get_policies()
    for addr in to_list:
        if not pol.is_allowed("email", addr):
            return {
                "error": (
                    f"recipient {addr!r} is not in the email allowlist; "
                    f"use pkg_imap_trust_sender to add."
                ),
            }

    expected = _resolve_expected_account()
    if not expected:
        return {"error": _EXPECTED_ACCOUNT_NOT_CONFIGURED_ERROR}

    raw_b64 = _build_raw_message(
        sender=expected, to=to_list, subject=subject, body=body,
    )

    state_seed = {
        "raw_message_b64": raw_b64,
        "to_addresses_json": json.dumps(list(to_list)),
    }
    if in_reply_to:
        state_seed["in_reply_to"] = in_reply_to

    return _create_write_arc_tree(
        template_name="email_write_send",
        arc_name=f"Email send: {subject[:60]}",
        arc_goal=(
            "Send an email via the smtp.send capability verb, then "
            "graduate a typed EmailSendResult receipt to trusted state "
            "via the constrained REVIEWER + deterministic JUDGE.  "
            "Recipients were validated against SecurityPolicies.email at "
            "the chat boundary; the trusted SMTP handler authenticates "
            "with the operator-confirmed credentials and uses the "
            "authenticated account as the envelope sender."
        ),
        script=SMTP_SEND_SCRIPT,
        state_seed=state_seed,
        expected_account_email=expected,
        staged_to_addresses=tuple(to_list),
        conversation_id=conversation_id,
    )


@chat_tool(
    description=(
        "Compose and send an email via SMTP (SMTPS).  Each ``to`` "
        "address is validated against the global SecurityPolicies.email "
        "allowlist before submission.  The trusted SMTP handler "
        "authenticates with the operator-confirmed account and uses it "
        "as the envelope sender, so a swapped credential cannot redirect "
        "the From.  Requires user confirmation at the chat boundary."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "to": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Recipient email addresses.  Each must be in the "
                    "SecurityPolicies.email allowlist (use "
                    "pkg_imap_trust_sender to add)."
                ),
            },
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["to", "subject", "body"],
    },
    capabilities=["arc_create", "external_effect"],
    requires_user_confirm=True,
)
def pkg_imap_send_email(tool_input, **kwargs):
    """Send an email through SMTP via the write-side arc tree."""
    result = _send_via_smtp(
        to_list=tool_input.get("to") or [],
        subject=(tool_input.get("subject") or "").strip(),
        body=tool_input.get("body") or "",
        in_reply_to=None,
        conversation_id=kwargs.get("conversation_id"),
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps({
        "arc_id": result["arc_id"],
        "note": (
            f"Send queued (arc #{result['arc_id']}).  Result arrives via "
            "arc-completion notify when the JUDGE approves the "
            "EmailSendResult receipt."
        ),
    })


@chat_tool(
    description=(
        "Reply to a message.  Same trust shape as pkg_imap_send_email "
        "(allowlist check + chat-boundary confirm + trusted SMTP "
        "handler), with an In-Reply-To reference recorded for threading. "
        "Requires user confirmation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "to": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Recipient addresses (must be allowlisted).",
            },
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "in_reply_to_message_id": {
                "type": "string",
                "description": (
                    "The Message-ID (or UID) of the message being "
                    "replied to, recorded for threading."
                ),
            },
        },
        "required": ["to", "subject", "body"],
    },
    capabilities=["arc_create", "external_effect"],
    requires_user_confirm=True,
)
def pkg_imap_reply_email(tool_input, **kwargs):
    """Reply to a message via SMTP (threading reference recorded)."""
    result = _send_via_smtp(
        to_list=tool_input.get("to") or [],
        subject=(tool_input.get("subject") or "").strip(),
        body=tool_input.get("body") or "",
        in_reply_to=(tool_input.get("in_reply_to_message_id") or "").strip() or None,
        conversation_id=kwargs.get("conversation_id"),
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps({
        "arc_id": result["arc_id"],
        "note": f"Reply queued (arc #{result['arc_id']}).",
    })


# ---------------------------------------------------------------------------
# External-effect modify: archive / mark-read
# ---------------------------------------------------------------------------


@chat_tool(
    description=(
        "Archive a message (flag it Archived + Seen via the imap.store "
        "capability verb).  Idempotent: archiving an already-archived "
        "message reports was_already_archived: true.  Trust shape "
        "mirrors pkg_imap_send_email — single untrusted EXECUTOR arc "
        "graduated through REVIEWER + JUDGE, chat-boundary human confirm. "
        "No recipient surface, so the allowlist is not consulted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider_message_id": {
                "type": "string",
                "description": "IMAP UID (decimal string).",
            },
        },
        "required": ["provider_message_id"],
    },
    capabilities=["arc_create", "external_effect"],
    requires_user_confirm=True,
)
def pkg_imap_archive_email(tool_input, **kwargs):
    """Archive one message via an EXECUTOR arc (imap.store)."""
    from .scripts import IMAP_ARCHIVE_SCRIPT

    mid = (tool_input.get("provider_message_id") or "").strip()
    if not mid:
        return json.dumps({"error": "provider_message_id (UID) is required"})
    expected = _resolve_expected_account()
    if not expected:
        return json.dumps({"error": _EXPECTED_ACCOUNT_NOT_CONFIGURED_ERROR})

    result = _create_write_arc_tree(
        template_name="email_write_archive",
        arc_name=f"Email archive: {mid[:32]}",
        arc_goal=(
            "Archive a message (flag Archived + Seen) via the imap.store "
            "capability verb.  REVIEWER extracts a typed "
            "EmailArchiveResult from the handler receipt; JUDGE "
            "graduates it from untrusted to trusted."
        ),
        script=IMAP_ARCHIVE_SCRIPT,
        state_seed={"provider_message_id": mid, "mailbox": _DEFAULT_MAILBOX},
        expected_account_email=expected,
        staged_to_addresses=(),
        conversation_id=kwargs.get("conversation_id"),
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps({
        "arc_id": result["arc_id"],
        "note": (
            f"Archive queued (arc #{result['arc_id']}); response includes "
            "was_already_archived for idempotency phrasing."
        ),
    })


@chat_tool(
    description=(
        "Mark a message as read (add the \\Seen flag via the imap.store "
        "capability verb).  Idempotent: marking an already-read message "
        "reports was_already_read: true.  Trust shape mirrors "
        "pkg_imap_send_email; requires user confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider_message_id": {
                "type": "string",
                "description": "IMAP UID (decimal string).",
            },
        },
        "required": ["provider_message_id"],
    },
    capabilities=["arc_create", "external_effect"],
    requires_user_confirm=True,
)
def pkg_imap_mark_read_email(tool_input, **kwargs):
    """Mark one message read via an EXECUTOR arc (imap.store)."""
    from .scripts import IMAP_MARK_READ_SCRIPT

    mid = (tool_input.get("provider_message_id") or "").strip()
    if not mid:
        return json.dumps({"error": "provider_message_id (UID) is required"})
    expected = _resolve_expected_account()
    if not expected:
        return json.dumps({"error": _EXPECTED_ACCOUNT_NOT_CONFIGURED_ERROR})

    result = _create_write_arc_tree(
        template_name="email_write_mark_read",
        arc_name=f"Email mark-read: {mid[:32]}",
        arc_goal=(
            "Mark a message as read (add \\Seen) via the imap.store "
            "capability verb.  REVIEWER extracts a typed "
            "EmailMarkReadResult from the handler receipt; JUDGE "
            "graduates it from untrusted to trusted."
        ),
        script=IMAP_MARK_READ_SCRIPT,
        state_seed={"provider_message_id": mid, "mailbox": _DEFAULT_MAILBOX},
        expected_account_email=expected,
        staged_to_addresses=(),
        conversation_id=kwargs.get("conversation_id"),
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps({
        "arc_id": result["arc_id"],
        "note": (
            f"Mark-read queued (arc #{result['arc_id']}); response "
            "includes was_already_read for idempotency phrasing."
        ),
    })


# ---------------------------------------------------------------------------
# Allowlist mutation (mirrors carpenter-gmail)
# ---------------------------------------------------------------------------


@chat_tool(
    description=(
        "Add an email address to the global SecurityPolicies.email "
        "allowlist.  After adding, that sender's messages can pass the "
        "EmailPolicy literal validation in extract dataclasses, and that "
        "recipient can be used as a ``to`` for pkg_imap_send_email.  "
        "Requires user confirmation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "email_address": {
                "type": "string",
                "description": "Email address to add to the allowlist.",
            },
            "reason": {
                "type": "string",
                "description": "Short audit note explaining the trust.",
            },
        },
        "required": ["email_address"],
    },
    capabilities=["database_write"],
    requires_user_confirm=True,
)
def pkg_imap_trust_sender(tool_input, **kwargs):
    """Add an EmailPolicy entry to the global email allowlist."""
    from carpenter.security import get_policies
    from carpenter.security import policy_store

    addr = (tool_input.get("email_address") or "").strip().lower()
    if not addr or "@" not in addr:
        return json.dumps({"error": "email_address must be a valid email"})
    try:
        policy_store.add_to_allowlist("email", addr)
        get_policies().add("email", addr)
    except Exception as exc:  # noqa: BLE001
        logger.exception("pkg_imap_trust_sender: add failed")
        return json.dumps({"error": f"add failed: {exc}"})
    return json.dumps({"accepted": True, "email": addr})


@chat_tool(
    description=(
        "Remove an email address from the SecurityPolicies.email "
        "allowlist.  Future messages from that sender will fail "
        "EmailPolicy validation (JUDGE will reject); future ``to`` uses "
        "will be blocked at the chat boundary."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "email_address": {"type": "string"},
        },
        "required": ["email_address"],
    },
    capabilities=["database_write"],
    requires_user_confirm=True,
)
def pkg_imap_untrust_sender(tool_input, **kwargs):
    """Remove an EmailPolicy entry from the allowlist."""
    from carpenter.security import get_policies
    from carpenter.security import policy_store

    addr = (tool_input.get("email_address") or "").strip().lower()
    if not addr:
        return json.dumps({"error": "email_address is required"})
    removed = policy_store.remove_from_allowlist("email", addr)
    get_policies().remove("email", addr)
    return json.dumps({"accepted": True, "removed": bool(removed), "email": addr})
