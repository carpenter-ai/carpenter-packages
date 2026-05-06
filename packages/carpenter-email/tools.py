"""Chat tools for the carpenter-email package.

These are the chat-boundary tools the chat agent calls.  Each is a
``@chat_tool``-decorated function.  Read-side tools fan out arc
batches into the package's PLANNER -> EXECUTOR -> REVIEWER -> JUDGE
pipelines; send-side ships ``pkg_email_send_email`` which creates a
single-arc untrusted EXECUTOR pipeline guarded by a chat-boundary
human-confirm and an in-script expected-account check.

Design notes (see ``docs/2026-05-06_carpenter-email-build-plan.md``
in carpenter-core):

* The chat agent never sees raw email bodies.  All read paths route
  through a templated REVIEWER + deterministic JUDGE that graduates
  a structured extract Resource to trusted state.
* ``pkg_email_send_email`` requires user confirmation at the chat
  boundary AND validates each ``to`` address against the global
  ``SecurityPolicies.email`` allowlist before submitting the EXECUTOR.
* Allowlist mutation (``pkg_email_trust_sender`` /
  ``pkg_email_untrust_sender``) goes through the platform's
  ``policy_store`` write path with ``requires_user_confirm=True`` so
  every entry is human-approved at chat time.
"""

from __future__ import annotations

import base64
import json
import logging
from email.message import EmailMessage

from carpenter.chat_tool_loader import chat_tool


logger = logging.getLogger(__name__)


_ENV_KEY_PREFIX = "GMAIL_OAUTH"


# ---------------------------------------------------------------------------
# Helpers (no @chat_tool — package-internal)
# ---------------------------------------------------------------------------


def _get_oauth_client_creds() -> tuple[str, str]:
    """Return the operator-supplied (client_id, client_secret) pair.

    Read from the platform .env via ``carpenter.config``.  The
    operator sets these once during install with the standard
    one-time-link credential UI; the platform writes them under
    ``GMAIL_OAUTH_CLIENT_ID`` and ``GMAIL_OAUTH_CLIENT_SECRET``.
    """
    from carpenter import config

    cid = (config.CONFIG.get("GMAIL_OAUTH_CLIENT_ID") or "").strip()
    sec = (config.CONFIG.get("GMAIL_OAUTH_CLIENT_SECRET") or "").strip()
    return cid, sec


def _create_read_arc_tree(
    *,
    template_name: str,
    provider_message_id: str,
    expected_account_email: str,
    conversation_id: int | None,
) -> dict:
    """Spin up the PLANNER -> EXECUTOR -> REVIEWER -> JUDGE arc tree
    for one Gmail message under one of our read templates.

    Returns ``{"arc_id": <parent_planner_id>}`` on success or
    ``{"error": ...}`` on failure.

    The Resource wiring mirrors the platform's ``_handle_fetch_web_content``
    pattern: a raw_email Resource (untrusted, produced_by_template=NULL)
    receives the EXECUTOR's Gmail JSON output; an extract Resource
    (template_verdict='pending', produced_by_template=<template_name>)
    is pre-created so the REVIEWER can derive into it and the JUDGE
    can flip its verdict via ``resource.submit_verdict``.

    The package author has audited the Gmail-fetch script in
    ``scripts.py`` once; the EXECUTOR is told to submit it verbatim
    via ``submit_code``.  This avoids handing the EXECUTOR an
    open-ended "go fetch this URL" goal where it would have to
    generate its own code.
    """
    from carpenter.core.arcs import manager as _am
    from carpenter.core.engine import work_queue as _wq
    from carpenter.core.resources import (
        create_resource as _create_resource,
        derive_resource as _derive_resource,
        link_arc_resource as _link_arc_resource,
        resource_storage_path as _resource_storage_path,
    )
    from carpenter.core.workflows._arc_state import set_arc_state
    from carpenter.db import db_transaction as _db_transaction
    from carpenter.tool_backends import arc as arc_backend

    from .scripts import GMAIL_FETCH_SCRIPT

    # 1) Parent PLANNER
    parent_id = _am.create_arc(
        name=f"Email read: {template_name}",
        goal=(
            "Construct an EmailReviewBriefing dataclass from the "
            "global SecurityPolicies.email allowlist snapshot and "
            "the package's static suspicious-keyword list, then "
            "derive_resource(kind='EmailReviewBriefing', verdict='approved') "
            "as a born-trusted Resource.  The EXECUTOR child has been "
            "pre-seeded with provider_message_id; the REVIEWER child "
            "will read the briefing + raw email JSON; the JUDGE is "
            "deterministic Python (no agent input needed)."
        ),
        agent_type="PLANNER",
    )
    if conversation_id:
        from carpenter.agent import conversation as _conv
        _conv.link_arc_to_conversation(conversation_id, parent_id)
    _am.update_status(parent_id, "active")

    # 2) Children: EXECUTOR (untrusted) + REVIEWER (constrained) + JUDGE
    batch_result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": f"Fetch Gmail message {provider_message_id[:16]}",
                "goal": (
                    "Submit this EXACT code via submit_code (do not "
                    "modify it):\n```python\n"
                    + GMAIL_FETCH_SCRIPT
                    + "```\nAll inputs (provider_message_id, "
                    "raw_resource_path, raw_resource_id) have been "
                    "pre-seeded in arc state."
                ),
                "parent_id": parent_id,
                "integrity_level": "untrusted",
                "output_type": "json",
                "agent_type": "EXECUTOR",
                "step_order": 0,
            },
            {
                "name": "Review email and emit extract",
                "goal": (
                    "Read the briefing Resource and the raw_email "
                    "Resource (paths in arc state under "
                    "'briefing_resource_id' and 'raw_resource_path'). "
                    "Follow the static REVIEWER prompt shipped with "
                    "this template.  Emit exactly one extract "
                    "dataclass via derive_resource using the kind "
                    "named in arc state under 'extract_kind'.  Then "
                    "exit — the deterministic JUDGE will validate "
                    "and graduate."
                ),
                "parent_id": parent_id,
                "agent_type": "REVIEWER",
                "integrity_level": "trusted",
                "reviewer_profile": "security-reviewer",
                "model_policy": "careful-coding",
                "step_order": 1,
            },
            {
                "name": "Judge extract",
                "goal": (
                    "JUDGE: validate the REVIEWER's extract Resource. "
                    "Call resource.submit_verdict with the extract's "
                    "resource_id (in arc state under "
                    "'_review_target_resource_id') and "
                    "verdict='approved' or 'rejected' based on the "
                    "package's deterministic JUDGE handler "
                    "(auto-dispatched by the platform via "
                    "_try_package_judge — you do not write Python "
                    "checks here)."
                ),
                "parent_id": parent_id,
                "agent_type": "JUDGE",
                "integrity_level": "trusted",
                "reviewer_profile": "judge",
                "step_order": 2,
            },
        ],
    })

    if "error" in batch_result:
        try:
            _am.update_status(parent_id, "failed")
        except Exception:  # noqa: BLE001
            pass
        return {"error": batch_result["error"]}

    child_ids = batch_result["arc_ids"]
    executor_arc_id, reviewer_arc_id, judge_arc_id = child_ids[:3]

    # 3) Resource wiring: raw_email Resource (untrusted ingest)
    raw_resource_id = _create_resource(
        content_type="json",
        file_path=None,
        produced_by_arc_id=executor_arc_id,
        source_descriptor=f"gmail:{provider_message_id}",
    )
    raw_path = _resource_storage_path(raw_resource_id, "blob")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with _db_transaction() as _db:
        _db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (str(raw_path), raw_resource_id),
        )
    _link_arc_resource(
        arc_id=executor_arc_id, resource_id=raw_resource_id, role="output",
    )

    # 4) Briefing Resource (PLANNER outputs; born-trusted by PLANNER)
    #    We pre-create the row so the PLANNER's derive_resource call
    #    can target a known id; the PLANNER fills in the bytes.
    briefing_resource_id = _derive_resource(
        content_type="dataclass",
        file_path=None,
        produced_by_arc_id=parent_id,
        produced_by_template=None,  # born trusted by trusted PLANNER
        template_verdict="approved",
        source_descriptor=f"briefing:{template_name}",
    )
    briefing_path = _resource_storage_path(briefing_resource_id, "blob")
    briefing_path.parent.mkdir(parents=True, exist_ok=True)
    with _db_transaction() as _db:
        _db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (str(briefing_path), briefing_resource_id),
        )

    # 5) Extract Resource (REVIEWER -> JUDGE; pending until JUDGE approves)
    extract_kind_by_template = {
        "email_read_simple_text": "EmailSimpleTextExtract",
        "email_read_meeting_invite": "EmailMeetingInviteExtract",
        "email_read_order_confirmation": "EmailOrderConfirmationExtract",
    }
    extract_kind = extract_kind_by_template[template_name]
    extract_resource_id = _derive_resource(
        content_type="dataclass",
        file_path=None,
        produced_by_arc_id=reviewer_arc_id,
        produced_by_template=template_name,
        template_verdict="pending",
        source_descriptor=f"extract:{provider_message_id}",
    )
    extract_path = _resource_storage_path(extract_resource_id, "blob")
    extract_path.parent.mkdir(parents=True, exist_ok=True)
    with _db_transaction() as _db:
        _db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (str(extract_path), extract_resource_id),
        )

    # 6) Arc-resource links
    _link_arc_resource(
        arc_id=reviewer_arc_id,
        resource_id=briefing_resource_id,
        role="input",
    )
    _link_arc_resource(
        arc_id=reviewer_arc_id,
        resource_id=raw_resource_id,
        role="input",
    )
    _link_arc_resource(
        arc_id=reviewer_arc_id,
        resource_id=extract_resource_id,
        role="output",
    )

    # 7) Pre-seed arc state
    set_arc_state(executor_arc_id, "provider_message_id", provider_message_id)
    set_arc_state(executor_arc_id, "raw_resource_path", str(raw_path))
    set_arc_state(executor_arc_id, "raw_resource_id", raw_resource_id)

    set_arc_state(parent_id, "expected_account_email", expected_account_email)
    set_arc_state(parent_id, "briefing_resource_id", briefing_resource_id)
    set_arc_state(parent_id, "_primary_resource_id", extract_resource_id)
    set_arc_state(parent_id, "template_name", template_name)
    set_arc_state(parent_id, "extract_kind", extract_kind)

    set_arc_state(reviewer_arc_id, "briefing_resource_id", briefing_resource_id)
    set_arc_state(reviewer_arc_id, "raw_resource_path", str(raw_path))
    set_arc_state(reviewer_arc_id, "raw_resource_id", raw_resource_id)
    set_arc_state(reviewer_arc_id, "extract_resource_id", extract_resource_id)
    set_arc_state(reviewer_arc_id, "extract_kind", extract_kind)
    set_arc_state(reviewer_arc_id, "template_name", template_name)

    set_arc_state(
        judge_arc_id, "_review_target_resource_id", extract_resource_id,
    )
    set_arc_state(judge_arc_id, "extract_resource_id", extract_resource_id)

    if conversation_id:
        from carpenter.agent import conversation as _conv
        for child_id in child_ids:
            _conv.link_arc_to_conversation(conversation_id, child_id)

    _wq.enqueue(
        "arc.dispatch",
        {"arc_id": executor_arc_id},
        idempotency_key=f"arc_dispatch:{executor_arc_id}",
    )
    return {"arc_id": parent_id}


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@chat_tool(
    description=(
        "Begin the Gmail OAuth flow.  Returns a one-time URL the "
        "user clicks in a browser to grant Carpenter access to their "
        "Gmail mailbox.  No email data is accessed by this call.  "
        "After the user completes the flow the access/refresh tokens "
        "are written to the platform .env under the GMAIL_OAUTH_ "
        "prefix and pkg_email_list_inbox / pkg_email_send_email "
        "become functional."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    capabilities=["external_effect"],
)
def pkg_email_authorize(tool_input, **kwargs):
    """Kick off the Google OAuth authorization-code flow.

    Reads the operator-supplied client_id / client_secret from the
    platform .env (set during install via the credential one-time
    link UI), then calls ``carpenter.api.oauth.start_flow`` with
    Gmail-appropriate scopes.  The returned URL is what the user
    actually clicks; the callback handler exchanges the code, writes
    tokens to .env under ``GMAIL_OAUTH_*``, and the user's chat
    session can then call read/send tools.
    """
    from carpenter.api import oauth as _oauth

    cid, sec = _get_oauth_client_creds()
    if not cid or not sec:
        return json.dumps({
            "error": (
                "GMAIL_OAUTH_CLIENT_ID / GMAIL_OAUTH_CLIENT_SECRET "
                "are not set in platform .env.  Operator must "
                "register a Google Cloud OAuth client (web "
                "application type) and import the credentials via "
                "the Carpenter credentials UI before this tool "
                "becomes functional."
            ),
        })
    try:
        result = _oauth.start_flow(
            provider="google",
            client_id=cid,
            client_secret=sec,
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=[
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
            env_key_prefix=_ENV_KEY_PREFIX,
            package_name="carpenter-email",
            extra_authorize_params={
                # Force Google to issue a refresh_token even on repeat consent.
                "access_type": "offline",
                "prompt": "consent",
            },
        )
        return json.dumps({
            "authorize_url": result["authorize_url"],
            "flow_id": result["flow_id"],
            "instructions": (
                "Open authorize_url in a browser, sign in with the "
                "Google account whose mail you want Carpenter to "
                "read, and grant the requested scopes.  The platform "
                "will write tokens to .env automatically.  After "
                "that you can call pkg_email_list_inbox / "
                "pkg_email_send_email."
            ),
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("pkg_email_authorize: start_flow failed")
        return json.dumps({"error": f"start_flow failed: {exc}"})


# ---------------------------------------------------------------------------
# Read-side: list / search / read
# ---------------------------------------------------------------------------


def _resolve_expected_account() -> str:
    """Return the mailbox email Carpenter expects to be acting on.

    Uses the userinfo cached from the OAuth flow if present; otherwise
    falls back to the platform's configured operator email.  Empty
    string is fine — JUDGE will then accept any allowlisted mailbox.
    """
    from carpenter import config

    return (
        config.CONFIG.get("GMAIL_OAUTH_ACCOUNT_EMAIL")
        or config.CONFIG.get("operator_email")
        or ""
    ).strip().lower()


def _create_search_executor(
    *,
    query: str,
    max_results: int,
    conversation_id: int | None,
) -> dict:
    """Run a single untrusted EXECUTOR that hits gmail.users.messages.list
    and writes the JSON result to a raw Resource.  The chat tool
    polls the resulting Resource (via the standard arc-completion
    notify path) and then fans out N child read-arc trees.

    Returns ``{"arc_id": <executor_id>}`` on success.
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

    from .scripts import GMAIL_SEARCH_SCRIPT

    parent_id = _am.create_arc(
        name=f"Email search: {query[:60]}",
        goal=(
            "Search Gmail for messages matching the query, then fan "
            "out N email_read_simple_text child arcs (one per "
            "matching message id).  Read the executor's id-list "
            "Resource (path in arc state under "
            "'id_list_resource_path') and create child arc trees via "
            "the package's _create_read_arc_tree helper."
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
                "name": f"List Gmail messages: {query[:40]}",
                "goal": (
                    "Submit this EXACT code via submit_code:\n"
                    "```python\n" + GMAIL_SEARCH_SCRIPT + "```\n"
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
        source_descriptor=f"gmail.search:{query}",
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


@chat_tool(
    description=(
        "Run a Gmail search and fan out one email_read_simple_text "
        "arc per matching message.  Returns the parent arc id; "
        "results arrive via the standard arc-completion notify "
        "channel.  The chat agent never sees raw email bodies — "
        "each child arc graduates a JUDGE-approved EmailSimpleTextExtract "
        "Resource that the agent reads via read_resource."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Gmail search syntax string, e.g. "
                    "\"newer_than:7d invoice\" or "
                    "\"from:billing@example.com\"."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": (
                    "Maximum number of messages to fetch.  Capped at "
                    "25 to bound work-queue load."
                ),
                "default": 10,
                "minimum": 1,
                "maximum": 25,
            },
        },
        "required": ["query"],
    },
    capabilities=["arc_create"],
)
def pkg_email_search_emails(tool_input, **kwargs):
    """Fan out an email_read_simple_text pipeline per matching message."""
    query = (tool_input.get("query") or "").strip()
    max_results = int(tool_input.get("max_results") or 10)
    if not query:
        return json.dumps({"error": "query is required"})
    if max_results < 1 or max_results > 25:
        return json.dumps({"error": "max_results must be between 1 and 25"})

    conversation_id = kwargs.get("conversation_id")
    result = _create_search_executor(
        query=query,
        max_results=max_results,
        conversation_id=conversation_id,
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps({
        "arc_id": result["arc_id"],
        "note": (
            "Search executor running.  When it completes, "
            "fan-out into per-message read pipelines is the "
            "responsibility of a follow-up planner step (Phase 1.5)."
        ),
    })


@chat_tool(
    description=(
        "Fetch the N most-recent inbox messages and run "
        "email_read_simple_text on each.  Returns the parent arc id; "
        "results arrive via the standard arc-completion notify path."
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
def pkg_email_list_inbox(tool_input, **kwargs):
    """Equivalent to pkg_email_search_emails(query='in:inbox')."""
    n = int(tool_input.get("max_results") or 10)
    return pkg_email_search_emails(
        {"query": "in:inbox", "max_results": n},
        **kwargs,
    )


@chat_tool(
    description=(
        "Read one specific Gmail message by provider message id.  "
        "Creates a single email_read_simple_text arc tree.  Returns "
        "the parent arc id; the JUDGE-approved EmailSimpleTextExtract "
        "Resource arrives via arc-completion notify."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider_message_id": {
                "type": "string",
                "description": "Gmail message id (opaque string).",
            },
            "kind": {
                "type": "string",
                "enum": [
                    "simple_text", "meeting_invite", "order_confirmation",
                ],
                "default": "simple_text",
                "description": (
                    "Which read template to use.  Default: simple_text."
                ),
            },
        },
        "required": ["provider_message_id"],
    },
    capabilities=["arc_create"],
)
def pkg_email_read_email(tool_input, **kwargs):
    """Run one read template on one message id."""
    mid = (tool_input.get("provider_message_id") or "").strip()
    if not mid:
        return json.dumps({"error": "provider_message_id is required"})
    kind = (tool_input.get("kind") or "simple_text").strip()
    template_name = {
        "simple_text": "email_read_simple_text",
        "meeting_invite": "email_read_meeting_invite",
        "order_confirmation": "email_read_order_confirmation",
    }.get(kind)
    if not template_name:
        return json.dumps({"error": f"unknown kind {kind!r}"})
    expected = _resolve_expected_account()
    conversation_id = kwargs.get("conversation_id")
    result = _create_read_arc_tree(
        template_name=template_name,
        provider_message_id=mid,
        expected_account_email=expected,
        conversation_id=conversation_id,
    )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Send-side: pkg_email_send_email
# ---------------------------------------------------------------------------


def _build_raw_message(
    *, sender: str, to: list[str], subject: str, body: str,
) -> str:
    """Build an RFC-822 message and base64url-encode it for the Gmail API."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(body)
    raw = msg.as_bytes()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@chat_tool(
    description=(
        "Compose and send an email via Gmail.  Each ``to`` address "
        "is validated against the global SecurityPolicies.email "
        "allowlist before submission; an in-script expected-account "
        "check verifies the OAuth token belongs to the configured "
        "operator mailbox.  Requires user confirmation at the chat "
        "boundary.  The chat agent NEVER sees inbound email bodies; "
        "this tool is for outbound-only composition."
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
                    "pkg_email_trust_sender to add)."
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
def pkg_email_send_email(tool_input, **kwargs):
    """Send an email through the Gmail API via an EXECUTOR arc.

    Arc shape: a single PLANNER -> EXECUTOR (untrusted, output_type='json').
    The PLANNER pre-validates recipients against SecurityPolicies.email;
    the EXECUTOR's pre-verified script (``GMAIL_SEND_SCRIPT``) checks
    that the OAuth token's account email matches expected_account_email
    before posting.

    The chat-boundary ``requires_user_confirm=True`` guarantees the
    user sees and approves every send before the EXECUTOR is dispatched.
    """
    from carpenter.core.arcs import manager as _am
    from carpenter.core.engine import work_queue as _wq
    from carpenter.core.workflows._arc_state import set_arc_state
    from carpenter.security import get_policies
    from carpenter.tool_backends import arc as arc_backend

    from .scripts import GMAIL_SEND_SCRIPT

    to_list = tool_input.get("to") or []
    subject = (tool_input.get("subject") or "").strip()
    body = tool_input.get("body") or ""

    if not isinstance(to_list, list) or not to_list:
        return json.dumps({"error": "to must be a non-empty list"})
    if not subject:
        return json.dumps({"error": "subject is required"})
    if not body:
        return json.dumps({"error": "body is required"})

    # Defence in depth: validate every recipient against the global
    # allowlist before we even build the arc.  Belt-and-braces: the
    # JUDGE-dispatch wrapper would reject on the EmailPolicy literal
    # too, but failing here gives the user a much better error.
    pol = get_policies()
    for addr in to_list:
        if not pol.is_allowed("email", addr):
            return json.dumps({
                "error": (
                    f"recipient {addr!r} is not in the email "
                    f"allowlist; use pkg_email_trust_sender to add."
                ),
            })

    expected = _resolve_expected_account()
    if not expected:
        return json.dumps({
            "error": (
                "operator_email / GMAIL_OAUTH_ACCOUNT_EMAIL not "
                "configured; cannot perform expected-account check"
            ),
        })

    raw_b64 = _build_raw_message(
        sender=expected, to=to_list, subject=subject, body=body,
    )

    # Single-arc EXECUTOR pipeline.  No REVIEWER+JUDGE because this is
    # an outbound effect, not a U->T promotion.  The expected-account
    # check inside the script is the trust gate.
    parent_id = _am.create_arc(
        name=f"Email send: {subject[:60]}",
        goal=(
            "Send an email via the Gmail API using the pre-verified "
            "send script.  Recipients have been validated against "
            "SecurityPolicies.email at the chat boundary; the "
            "in-script expected-account check guards against a "
            "swapped-in refresh-token attack."
        ),
        agent_type="PLANNER",
    )
    conversation_id = kwargs.get("conversation_id")
    if conversation_id:
        from carpenter.agent import conversation as _conv
        _conv.link_arc_to_conversation(conversation_id, parent_id)
    _am.update_status(parent_id, "active")

    batch_result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": "Send Gmail message",
                "goal": (
                    "Submit this EXACT code via submit_code:\n"
                    "```python\n" + GMAIL_SEND_SCRIPT + "```\n"
                    "Inputs (raw_message_b64, expected_account_email) "
                    "are pre-seeded in arc state."
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
        return json.dumps({"error": batch_result["error"]})

    executor_arc_id = batch_result["arc_ids"][0]
    set_arc_state(executor_arc_id, "raw_message_b64", raw_b64)
    set_arc_state(executor_arc_id, "expected_account_email", expected)
    set_arc_state(parent_id, "expected_account_email", expected)

    if conversation_id:
        from carpenter.agent import conversation as _conv
        _conv.link_arc_to_conversation(conversation_id, executor_arc_id)

    _wq.enqueue(
        "arc.dispatch",
        {"arc_id": executor_arc_id},
        idempotency_key=f"arc_dispatch:{executor_arc_id}",
    )
    return json.dumps({
        "arc_id": parent_id,
        "note": (
            f"Send queued (arc #{parent_id}).  Result will arrive via "
            "arc-completion notify."
        ),
    })


# ---------------------------------------------------------------------------
# Allowlist mutation
# ---------------------------------------------------------------------------


@chat_tool(
    description=(
        "Add an email address to the global SecurityPolicies.email "
        "allowlist.  After adding, that sender's messages can pass "
        "the EmailPolicy literal validation in extract dataclasses, "
        "and that recipient can be used as a ``to`` for "
        "pkg_email_send_email.  Requires user confirmation."
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
                "description": (
                    "Short note for the audit log explaining why this "
                    "address is being trusted."
                ),
            },
        },
        "required": ["email_address"],
    },
    capabilities=["database_write"],
    requires_user_confirm=True,
)
def pkg_email_trust_sender(tool_input, **kwargs):
    """Add an EmailPolicy entry to the global email allowlist."""
    from carpenter.security import get_policies
    from carpenter.security import policy_store

    addr = (tool_input.get("email_address") or "").strip().lower()
    if not addr or "@" not in addr:
        return json.dumps({"error": "email_address must be a valid email"})

    try:
        policy_store.add_to_allowlist("email", addr)
        # Refresh the in-memory singleton so the new entry is live
        # for the next dataclass-construction validation pass.
        get_policies().add("email", addr)
    except Exception as exc:  # noqa: BLE001
        logger.exception("pkg_email_trust_sender: add failed")
        return json.dumps({"error": f"add failed: {exc}"})
    return json.dumps({"accepted": True, "email": addr})


@chat_tool(
    description=(
        "Remove an email address from the SecurityPolicies.email "
        "allowlist.  Future messages from that sender will fail "
        "EmailPolicy validation in extract dataclasses (JUDGE will "
        "reject); future ``to`` uses will be blocked at the chat "
        "boundary."
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
def pkg_email_untrust_sender(tool_input, **kwargs):
    """Remove an EmailPolicy entry from the allowlist."""
    from carpenter.security import get_policies
    from carpenter.security import policy_store

    addr = (tool_input.get("email_address") or "").strip().lower()
    if not addr:
        return json.dumps({"error": "email_address is required"})

    removed = policy_store.remove_from_allowlist("email", addr)
    get_policies().remove("email", addr)
    return json.dumps({"accepted": True, "removed": bool(removed), "email": addr})
