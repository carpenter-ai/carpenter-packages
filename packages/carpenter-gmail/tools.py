"""Chat tools for the carpenter-gmail package.

These are the chat-boundary tools the chat agent calls.  Each is a
``@chat_tool``-decorated function.  Read-side and write-side tools
both fan out arc batches into the package's PLANNER -> EXECUTOR ->
REVIEWER -> JUDGE pipeline so every U->T graduation passes through a
deterministic JUDGE handler.

Design notes (see ``docs/design.md`` for the full architecture write-up):

* The chat agent never sees raw email bodies.  All read paths route
  through a templated REVIEWER + deterministic JUDGE that graduates
  a structured extract Resource to trusted state.
* The four external-effect tools (``pkg_gmail_send_email``,
  ``pkg_gmail_archive_email``, ``pkg_gmail_mark_read_email``,
  ``pkg_gmail_draft_email``) each require user confirmation at the
  chat boundary AND go through the same four-arc tree as reads.  The
  EXECUTOR's pre-verified script writes a small structured JSON
  receipt; the REVIEWER + JUDGE graduate a typed EmailXxxResult
  dataclass so the chat agent can phrase the outcome without ever
  consuming Gmail's response body directly.
* ``pkg_gmail_send_email`` and ``pkg_gmail_draft_email`` additionally
  validate each ``to`` address against the global
  ``SecurityPolicies.email`` allowlist at the chat boundary BEFORE
  the arc is built.
* Allowlist mutation (``pkg_gmail_trust_sender`` /
  ``pkg_gmail_untrust_sender``) goes through the platform's
  ``policy_store`` write path with ``requires_user_confirm=True`` so
  every entry is human-approved at chat time.
"""

from __future__ import annotations

import json
import logging

from carpenter.chat_tool_loader import chat_tool

# Backend-agnostic arc-tree builders + extract-kind maps + the RFC-822
# raw-message helper, composed in from the carpenter-email-core layer
# (see layers/carpenter-email-core/arc_builders.py).  Gmail binds its
# own pre-verified scripts and source prefix into these via the thin
# wrappers below; the builders themselves name no backend.
from .arc_builders import (
    EXTRACT_KIND_BY_TEMPLATE as _EXTRACT_KIND_BY_TEMPLATE,  # noqa: F401
    _WRITE_EXTRACT_KIND_BY_TEMPLATE,  # noqa: F401
    _build_raw_message,
    _create_index_arc_tree as _create_index_arc_tree_base,
    _create_read_arc_tree as _create_read_arc_tree_base,
    _create_triage_arc_tree as _create_triage_arc_tree_base,
    _create_write_arc_tree as _create_write_arc_tree_base,
)


logger = logging.getLogger(__name__)


_ENV_KEY_PREFIX = "GMAIL_OAUTH"

# Audit ``source_descriptor`` prefix for untrusted raw-fetch Resources
# created by this backend.  Passed into the shared arc builders so the
# on-disk descriptors stay ``gmail:...`` / ``gmail-write:...`` /
# ``gmail.index....`` exactly as before the layer extraction.
_GMAIL_SOURCE_PREFIX = "gmail"


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
    """Gmail read arc tree: bind the Gmail fetch script + source prefix
    into the shared, backend-agnostic builder in ``arc_builders``."""
    from .scripts import GMAIL_FETCH_SCRIPT

    return _create_read_arc_tree_base(
        template_name=template_name,
        provider_message_id=provider_message_id,
        expected_account_email=expected_account_email,
        conversation_id=conversation_id,
        fetch_script=GMAIL_FETCH_SCRIPT,
        owner_package="carpenter-gmail",
        raw_source_prefix=_GMAIL_SOURCE_PREFIX,
    )


def _create_triage_arc_tree(
    *,
    provider_message_id: str,
    received_history_id: str,
    expected_account_email: str,
) -> dict:
    """Gmail triage arc tree: bind the Gmail fetch script + source
    prefix into the shared builder in ``arc_builders``.

    Called from the ``email.received`` subscription handler
    (handlers/triage_inbound.py) via a relative ``from ..tools import``;
    keeping this wrapper in ``tools`` is what lets the layer's handler
    stay backend-agnostic."""
    from .scripts import GMAIL_FETCH_SCRIPT

    return _create_triage_arc_tree_base(
        provider_message_id=provider_message_id,
        received_history_id=received_history_id,
        expected_account_email=expected_account_email,
        fetch_script=GMAIL_FETCH_SCRIPT,
        owner_package="carpenter-gmail",
        raw_source_prefix=_GMAIL_SOURCE_PREFIX,
    )


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
        "prefix and pkg_gmail_list_inbox / pkg_gmail_send_email "
        "become functional."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    capabilities=["external_effect"],
)
def pkg_gmail_authorize(tool_input, **kwargs):
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
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
            env_key_prefix=_ENV_KEY_PREFIX,
            package_name="carpenter-gmail",
            extra_authorize_params={
                # Force Google to issue a refresh_token even on repeat consent.
                "access_type": "offline",
                "prompt": "consent",
                # Phase 1.5 OAuth-migration helper: incrementally augment
                # any existing v0.1.0 grant (gmail.readonly + gmail.send +
                # userinfo.email) with the new gmail.modify + gmail.compose
                # scopes rather than replacing the existing grant.  See
                # SETUP.md for the user-facing migration walk-through.
                "include_granted_scopes": "true",
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
                "that you can call pkg_gmail_list_inbox / "
                "pkg_gmail_send_email."
            ),
        })
    except Exception as exc:  # noqa: BLE001
        logger.exception("pkg_gmail_authorize: start_flow failed")
        return json.dumps({"error": f"start_flow failed: {exc}"})


# ---------------------------------------------------------------------------
# Read-side: list / search / read
# ---------------------------------------------------------------------------


def _resolve_expected_account() -> str:
    """Return the mailbox email Carpenter expects to be acting on.

    Uses the userinfo cached from the OAuth flow if present; otherwise
    falls back to the platform's configured operator email.  Returns
    empty string if neither is set; callers MUST treat empty as a
    fail-closed condition (T1 envelope check is unenforceable without
    an expected account).
    """
    from carpenter import config

    return (
        config.CONFIG.get("GMAIL_OAUTH_ACCOUNT_EMAIL")
        or config.CONFIG.get("operator_email")
        or ""
    ).strip().lower()


_EXPECTED_ACCOUNT_NOT_CONFIGURED_ERROR = (
    "expected_account is not configured (neither GMAIL_OAUTH_ACCOUNT_EMAIL "
    "nor operator_email is set).  Run pkg_gmail_authorize first to "
    "complete the Gmail OAuth flow — without this, the T1 envelope "
    "recipient-mismatch check cannot be enforced and read paths fail "
    "closed."
)


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


def _index_status_snapshot() -> dict:
    """Return a small structured snapshot of the indexer's progress
    for inclusion in pkg_gmail_search_emails responses.

    The chat agent surfaces this to the user so they understand
    whether a search hit zero results because nothing matched OR
    because the index has not yet covered that mailbox region.
    Keys: ``vector_count``, ``phase1_complete``, ``incremental_ready``,
    ``paused``.
    """
    out = {
        "vector_count": 0,
        "phase1_complete": False,
        "incremental_ready": False,
        "paused": False,
    }
    try:
        from carpenter.packages.state import PackageStateHandle
        from carpenter.packages.vectors import PackageVectorStore
    except ImportError:
        return out
    try:
        out["vector_count"] = PackageVectorStore("carpenter-gmail").count()
    except Exception:
        pass
    pkg_state = PackageStateHandle("carpenter-gmail")
    try:
        out["phase1_complete"] = bool(
            pkg_state.get("index_phase1_completed_at"),
        )
        out["incremental_ready"] = bool(
            pkg_state.get("index_incremental_watermark"),
        )
        paused_raw = pkg_state.get("index_paused")
        out["paused"] = bool(paused_raw)
    except Exception:
        pass
    return out


def _vector_search(
    query_text: str, max_results: int,
) -> list[dict] | None:
    """Embed *query_text* and search the carpenter-gmail vector
    namespace.  Returns a list of dicts ``{"provider_message_id":
    str, "score": float, "metadata": dict}`` or ``None`` if vectors
    are unavailable in this build.
    """
    try:
        from carpenter.packages.vectors import PackageVectorStore
    except ImportError:
        return None
    try:
        vectors = PackageVectorStore("carpenter-gmail")
        hits = vectors.embed_and_search(query_text, top_k=max_results)
    except Exception:  # noqa: BLE001 — vector failures are not fatal
        logger.exception(
            "pkg_gmail_search_emails: vector backend failed; falling "
            "back to keyword search",
        )
        return None
    out: list[dict] = []
    for vid, score, metadata in hits:
        # Vector id is the provider_message_id by convention (the
        # trigger upserts under this id).  Metadata floats are NEVER
        # in trusted-context strings — E1 invariant.  We pass only
        # the validated metadata fields the JUDGE graduated.
        out.append({
            "provider_message_id": str(vid),
            "score": float(score),
            "metadata": dict(metadata or {}),
        })
    return out


@chat_tool(
    description=(
        "Search Gmail.  When the semantic resource index has covered "
        "your mailbox (Phase 1 backfill complete) and the query "
        "looks natural-language, returns vector-search hits "
        "directly — no Gmail API round-trip needed.  Otherwise falls "
        "back to a Gmail keyword search that fans out one "
        "email_read_simple_text arc per matching message.  Returns "
        "either the vector hits inline OR a parent arc id; in both "
        "cases the chat agent never sees raw email bodies."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-form search string.  Gmail search syntax "
                    "(e.g. \"newer_than:7d invoice\") is honored on "
                    "the keyword path; the vector path treats the "
                    "string as natural language."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": (
                    "Maximum number of results.  Capped at 25 to "
                    "bound work-queue load."
                ),
                "default": 10,
                "minimum": 1,
                "maximum": 25,
            },
            "backend": {
                "type": "string",
                "enum": ["auto", "vector", "keyword"],
                "default": "auto",
                "description": (
                    "Which backend to use.  ``auto`` picks vector "
                    "when the index is populated and the query has "
                    "no Gmail-specific operators (\"from:\", "
                    "\"newer_than:\", \"in:\", etc), keyword "
                    "otherwise.  ``vector`` forces vector (returns "
                    "empty hits if the index is unpopulated). "
                    "``keyword`` forces the Gmail-API path."
                ),
            },
        },
        "required": ["query"],
    },
    capabilities=["arc_create"],
)
def pkg_gmail_search_emails(tool_input, **kwargs):
    """Vector-or-keyword email search with index-status surfacing."""
    query = (tool_input.get("query") or "").strip()
    max_results = int(tool_input.get("max_results") or 10)
    backend = (tool_input.get("backend") or "auto").strip().lower()
    if not query:
        return json.dumps({"error": "query is required"})
    if max_results < 1 or max_results > 25:
        return json.dumps({"error": "max_results must be between 1 and 25"})
    if backend not in ("auto", "vector", "keyword"):
        return json.dumps({"error": "backend must be one of auto/vector/keyword"})
    if not _resolve_expected_account():
        return json.dumps({"error": _EXPECTED_ACCOUNT_NOT_CONFIGURED_ERROR})

    index_status = _index_status_snapshot()

    # Decide which backend to use.
    has_gmail_op = any(
        op in query.lower()
        for op in (
            "from:", "to:", "cc:", "bcc:", "subject:",
            "newer_than:", "older_than:", "in:", "label:",
            "has:", "filename:", "after:", "before:",
        )
    )
    use_vector = False
    if backend == "vector":
        use_vector = True
    elif backend == "auto":
        # Auto: vector only if Phase 1 done, index non-empty, and the
        # query is not in Gmail-operator form.
        use_vector = (
            index_status.get("vector_count", 0) > 0
            and index_status.get("phase1_complete", False)
            and not has_gmail_op
        )

    if use_vector:
        hits = _vector_search(query, max_results)
        if hits is None:
            # Vector unavailable — fall through to keyword only if
            # user did not explicitly force vector.
            if backend == "vector":
                return json.dumps({
                    "error": "vector backend unavailable in this build",
                    "index_status": index_status,
                })
            use_vector = False
        else:
            return json.dumps({
                "backend": "vector",
                "hits": hits,
                "index_status": index_status,
                "note": (
                    f"{len(hits)} vector hit(s) returned without a "
                    "Gmail API round-trip.  Pass a provider_message_id "
                    "to pkg_gmail_read_email to graduate a typed "
                    "extract Resource."
                ),
            })

    # Keyword fallback: existing Phase 1 behaviour.
    conversation_id = kwargs.get("conversation_id")
    result = _create_search_executor(
        query=query,
        max_results=max_results,
        conversation_id=conversation_id,
    )
    if "error" in result:
        return json.dumps({**result, "index_status": index_status})
    return json.dumps({
        "backend": "keyword",
        "arc_id": result["arc_id"],
        "index_status": index_status,
        "note": (
            "Search executor running.  When it completes, "
            "fan-out into per-message read pipelines is the "
            "responsibility of a follow-up planner step."
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
def pkg_gmail_list_inbox(tool_input, **kwargs):
    """Equivalent to pkg_gmail_search_emails(query='in:inbox')."""
    n = int(tool_input.get("max_results") or 10)
    return pkg_gmail_search_emails(
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
def pkg_gmail_read_email(tool_input, **kwargs):
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
    if not expected:
        return json.dumps({"error": _EXPECTED_ACCOUNT_NOT_CONFIGURED_ERROR})
    conversation_id = kwargs.get("conversation_id")
    result = _create_read_arc_tree(
        template_name=template_name,
        provider_message_id=mid,
        expected_account_email=expected,
        conversation_id=conversation_id,
    )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Send-side: pkg_gmail_send_email
# ---------------------------------------------------------------------------


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
                    "pkg_gmail_trust_sender to add)."
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
def pkg_gmail_send_email(tool_input, **kwargs):
    """Send an email through the Gmail API via the write-side arc tree.

    Arc shape: PLANNER -> EXECUTOR (untrusted) -> REVIEWER ->
    JUDGE, identical to the read tools.  The EXECUTOR's pre-verified
    script (``GMAIL_SEND_SCRIPT``) checks that the OAuth token's
    account email matches expected_account_email before posting and
    writes a structured JSON receipt for the REVIEWER + JUDGE to
    graduate as an EmailSendResult.

    Recipients are validated against ``SecurityPolicies.email`` at the
    chat boundary BEFORE the arc is constructed; the chat-boundary
    ``requires_user_confirm=True`` guarantees the user sees and
    approves every send.
    """
    from carpenter.security import get_policies

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
                    f"allowlist; use pkg_gmail_trust_sender to add."
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

    result = _create_write_arc_tree(
        template_name="email_write_send",
        arc_name=f"Email send: {subject[:60]}",
        arc_goal=(
            "Send an email via the Gmail API using the pre-verified "
            "send script, then graduate a typed EmailSendResult "
            "receipt to trusted state via the constrained REVIEWER + "
            "deterministic JUDGE.  Recipients were validated against "
            "SecurityPolicies.email at the chat boundary; the "
            "in-script expected-account check guards against a "
            "swapped-in refresh-token attack."
        ),
        script=GMAIL_SEND_SCRIPT,
        state_seed={"raw_message_b64": raw_b64},
        expected_account_email=expected,
        staged_to_addresses=tuple(to_list),
        conversation_id=kwargs.get("conversation_id"),
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps({
        "arc_id": result["arc_id"],
        "note": (
            f"Send queued (arc #{result['arc_id']}).  Result will "
            "arrive via arc-completion notify when the JUDGE "
            "approves the EmailSendResult receipt."
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
        "pkg_gmail_send_email.  Requires user confirmation."
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
def pkg_gmail_trust_sender(tool_input, **kwargs):
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
        logger.exception("pkg_gmail_trust_sender: add failed")
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
def pkg_gmail_untrust_sender(tool_input, **kwargs):
    """Remove an EmailPolicy entry from the allowlist."""
    from carpenter.security import get_policies
    from carpenter.security import policy_store

    addr = (tool_input.get("email_address") or "").strip().lower()
    if not addr:
        return json.dumps({"error": "email_address is required"})

    removed = policy_store.remove_from_allowlist("email", addr)
    get_policies().remove("email", addr)
    return json.dumps({"accepted": True, "removed": bool(removed), "email": addr})


# ---------------------------------------------------------------------------
# Write-side helper: PLANNER -> EXECUTOR -> REVIEWER -> JUDGE pipeline
#
# All four external-effect tools (send + archive + mark-read + draft)
# share the same arc-tree shape as the read tools.  Each EXECUTOR script
# writes a small structured JSON receipt to a raw Resource; the REVIEWER
# derives a typed EmailXxxResult dataclass into a pending extract
# Resource; the package JUDGE validates and graduates.  Trust invariant
# I3 (U->T only via JUDGE) is preserved by construction.
# ---------------------------------------------------------------------------


# Map each write template to its extract dataclass kind.  Used to
# pre-create the pending extract Resource the REVIEWER derives into.


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
    """Gmail write arc tree: delegate to the shared builder in
    ``arc_builders`` with the Gmail source prefix.  ``script`` is the
    pre-verified write script the caller already resolved from
    ``scripts.py``."""
    return _create_write_arc_tree_base(
        template_name=template_name,
        arc_name=arc_name,
        arc_goal=arc_goal,
        script=script,
        state_seed=state_seed,
        expected_account_email=expected_account_email,
        staged_to_addresses=staged_to_addresses,
        conversation_id=conversation_id,
        owner_package="carpenter-gmail",
        raw_source_prefix=_GMAIL_SOURCE_PREFIX,
    )


@chat_tool(
    description=(
        "Archive a Gmail message (remove the INBOX label).  Idempotent: "
        "archiving an already-archived message returns "
        "{archived: true, was_already_archived: true}.  Trust shape "
        "mirrors pkg_gmail_send_email — single untrusted EXECUTOR arc, "
        "chat-boundary human confirm, in-script expected-account check.  "
        "Allowlist is NOT consulted because no recipient surface exists; "
        "this only mutates own-inbox state."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider_message_id": {
                "type": "string",
                "description": "Gmail message id (opaque string).",
            },
        },
        "required": ["provider_message_id"],
    },
    capabilities=["arc_create", "external_effect"],
    requires_user_confirm=True,
)
def pkg_gmail_archive_email(tool_input, **kwargs):
    """Archive one Gmail message via an EXECUTOR arc."""
    from .scripts import GMAIL_ARCHIVE_SCRIPT

    mid = (tool_input.get("provider_message_id") or "").strip()
    if not mid:
        return json.dumps({"error": "provider_message_id is required"})

    expected = _resolve_expected_account()
    if not expected:
        return json.dumps({
            "error": (
                "operator_email / GMAIL_OAUTH_ACCOUNT_EMAIL not "
                "configured; cannot perform expected-account check.  "
                "Run pkg_gmail_authorize first."
            ),
        })

    result = _create_write_arc_tree(
        template_name="email_write_archive",
        arc_name=f"Email archive: {mid[:32]}",
        arc_goal=(
            "Archive a Gmail message (remove INBOX label) using the "
            "pre-verified archive script.  The in-script "
            "expected-account check guards against a swapped-in "
            "refresh-token attack.  REVIEWER extracts a typed "
            "EmailArchiveResult from the Gmail response; JUDGE "
            "graduates it from untrusted to trusted."
        ),
        script=GMAIL_ARCHIVE_SCRIPT,
        state_seed={"provider_message_id": mid},
        expected_account_email=expected,
        staged_to_addresses=(),
        conversation_id=kwargs.get("conversation_id"),
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps({
        "arc_id": result["arc_id"],
        "note": (
            f"Archive queued (arc #{result['arc_id']}).  Result will "
            "arrive via arc-completion notify; the response message "
            "includes was_already_archived for idempotency phrasing."
        ),
    })


@chat_tool(
    description=(
        "Mark a Gmail message as read (remove the UNREAD label).  "
        "Idempotent: marking an already-read message returns "
        "{marked_read: true, was_already_read: true}.  Trust shape "
        "mirrors pkg_gmail_send_email — single untrusted EXECUTOR arc, "
        "chat-boundary human confirm, in-script expected-account check."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider_message_id": {
                "type": "string",
                "description": "Gmail message id (opaque string).",
            },
        },
        "required": ["provider_message_id"],
    },
    capabilities=["arc_create", "external_effect"],
    requires_user_confirm=True,
)
def pkg_gmail_mark_read_email(tool_input, **kwargs):
    """Mark one Gmail message as read via an EXECUTOR arc."""
    from .scripts import GMAIL_MARK_READ_SCRIPT

    mid = (tool_input.get("provider_message_id") or "").strip()
    if not mid:
        return json.dumps({"error": "provider_message_id is required"})

    expected = _resolve_expected_account()
    if not expected:
        return json.dumps({
            "error": (
                "operator_email / GMAIL_OAUTH_ACCOUNT_EMAIL not "
                "configured; cannot perform expected-account check.  "
                "Run pkg_gmail_authorize first."
            ),
        })

    result = _create_write_arc_tree(
        template_name="email_write_mark_read",
        arc_name=f"Email mark-read: {mid[:32]}",
        arc_goal=(
            "Mark a Gmail message as read (remove UNREAD label) using "
            "the pre-verified mark-read script.  The in-script "
            "expected-account check guards against a swapped-in "
            "refresh-token attack.  REVIEWER extracts a typed "
            "EmailMarkReadResult from the Gmail response; JUDGE "
            "graduates it from untrusted to trusted."
        ),
        script=GMAIL_MARK_READ_SCRIPT,
        state_seed={"provider_message_id": mid},
        expected_account_email=expected,
        staged_to_addresses=(),
        conversation_id=kwargs.get("conversation_id"),
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps({
        "arc_id": result["arc_id"],
        "note": (
            f"Mark-read queued (arc #{result['arc_id']}).  Result will "
            "arrive via arc-completion notify; the response message "
            "includes was_already_read for idempotency phrasing."
        ),
    })


@chat_tool(
    description=(
        "Create a Gmail draft (the message is staged, NOT sent).  "
        "Recipients are validated against SecurityPolicies.email at "
        "draft-creation time — a draft with un-allowlisted recipients "
        "would be a foothold for a later send-bypass and is refused "
        "up-front.  Requires user confirmation at the chat boundary.  "
        "Each call creates a NEW draft; there is no update-draft tool "
        "in Phase 1.5 because sending a stale draft would bypass the "
        "re-confirm on body content (send the draft by re-running "
        "pkg_gmail_send_email with the same body)."
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
                    "pkg_gmail_trust_sender to add)."
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
def pkg_gmail_draft_email(tool_input, **kwargs):
    """Create a Gmail draft via an EXECUTOR arc."""
    from carpenter.security import get_policies

    from .scripts import GMAIL_DRAFT_SCRIPT

    to_list = tool_input.get("to") or []
    subject = (tool_input.get("subject") or "").strip()
    body = tool_input.get("body") or ""

    if not isinstance(to_list, list) or not to_list:
        return json.dumps({"error": "to must be a non-empty list"})
    if not subject:
        return json.dumps({"error": "subject is required"})
    if not body:
        return json.dumps({"error": "body is required"})

    # Mirror pkg_gmail_send_email: validate every recipient against the
    # global allowlist BEFORE creating the arc.  Drafts with un-
    # allowlisted addresses would be a foothold for a later
    # send-bypass and are refused up-front.
    pol = get_policies()
    for addr in to_list:
        if not pol.is_allowed("email", addr):
            return json.dumps({
                "error": (
                    f"recipient {addr!r} is not in the email "
                    f"allowlist; use pkg_gmail_trust_sender to add."
                ),
            })

    expected = _resolve_expected_account()
    if not expected:
        return json.dumps({
            "error": (
                "operator_email / GMAIL_OAUTH_ACCOUNT_EMAIL not "
                "configured; cannot perform expected-account check.  "
                "Run pkg_gmail_authorize first."
            ),
        })

    raw_b64 = _build_raw_message(
        sender=expected, to=to_list, subject=subject, body=body,
    )

    result = _create_write_arc_tree(
        template_name="email_write_draft",
        arc_name=f"Email draft: {subject[:60]}",
        arc_goal=(
            "Create a Gmail draft using the pre-verified draft "
            "script.  Recipients have been validated against "
            "SecurityPolicies.email at the chat boundary; the "
            "in-script expected-account check guards against a "
            "swapped-in refresh-token attack.  REVIEWER extracts a "
            "typed EmailDraftResult from the Gmail response; JUDGE "
            "graduates it from untrusted to trusted."
        ),
        script=GMAIL_DRAFT_SCRIPT,
        state_seed={"raw_message_b64": raw_b64},
        expected_account_email=expected,
        staged_to_addresses=tuple(to_list),
        conversation_id=kwargs.get("conversation_id"),
    )
    if "error" in result:
        return json.dumps(result)
    return json.dumps({
        "arc_id": result["arc_id"],
        "note": (
            f"Draft queued (arc #{result['arc_id']}).  Result will "
            "arrive via arc-completion notify; the response message "
            "includes the Gmail-assigned draft_id and "
            "provider_message_id."
        ),
    })


# ---------------------------------------------------------------------------
# Phase 4: semantic resource index — arc-tree helper + chat tools
# ---------------------------------------------------------------------------


# Maps Phase-4 template name → (extract_kind, script).
def _index_template_meta(template_name: str) -> tuple[str, str]:
    from .scripts import (
        GMAIL_INDEX_INCREMENTAL_SCRIPT,
        GMAIL_INDEX_PHASE1_SCRIPT,
        GMAIL_INDEX_PHASE2_SCRIPT,
    )
    table = {
        "email_index_phase1":      ("EmailIndexFetchedBatch", GMAIL_INDEX_PHASE1_SCRIPT),
        "email_index_phase2":      ("EmailIndexFetchedBatch", GMAIL_INDEX_PHASE2_SCRIPT),
        "email_index_incremental": ("EmailIndexFetchedBatch", GMAIL_INDEX_INCREMENTAL_SCRIPT),
    }
    return table[template_name]


def _create_index_arc_tree(
    *,
    template_name: str,
    phase: str,
    batch_id: str,
    watermark_before: str,
    expected_account_email: str,
    model_identity: str,
    executor_state_seed: dict,
) -> dict:
    """Gmail index arc tree: resolve the phase-specific Gmail script +
    extract kind via ``_index_template_meta`` and delegate to the
    shared builder in ``arc_builders`` with the Gmail source prefix."""
    extract_kind, script = _index_template_meta(template_name)
    return _create_index_arc_tree_base(
        template_name=template_name,
        phase=phase,
        batch_id=batch_id,
        watermark_before=watermark_before,
        expected_account_email=expected_account_email,
        model_identity=model_identity,
        executor_state_seed=executor_state_seed,
        script=script,
        extract_kind=extract_kind,
        owner_package="carpenter-gmail",
        raw_source_prefix=_GMAIL_SOURCE_PREFIX,
    )


# ---------------------------------------------------------------------------
# Phase 4 chat tools
# ---------------------------------------------------------------------------


@chat_tool(
    description=(
        "Force a re-index of the email semantic resource index.  Wipes "
        "the package's vector namespace and resets all three indexer "
        "watermarks so Phase 1 backfill restarts from the top of the "
        "mailbox.  Requires user confirmation at the chat boundary.  "
        "The indexer triggers pick up the work on their next 60-second "
        "tick; no arc is spawned synchronously."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": (
                    "Short free-text reason for the reindex (logged "
                    "to package_state for audit; <= 256 chars)."
                ),
                "maxLength": 256,
            },
        },
        "required": [],
    },
    requires_user_confirm=True,
    capabilities=[],
)
def pkg_gmail_reindex(tool_input, **kwargs):
    """Wipe the email vector namespace and reset indexer watermarks."""
    reason = (tool_input.get("reason") or "").strip()[:256]
    try:
        from carpenter.packages.state import PackageStateHandle
        from carpenter.packages.vectors import PackageVectorStore
    except ImportError:
        return json.dumps({"error": "package.state / package.vectors unavailable"})
    pkg_state = PackageStateHandle("carpenter-gmail")
    vectors = PackageVectorStore("carpenter-gmail")
    try:
        cleared = vectors.clear()
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"vector clear failed: {exc}"})
    # Wipe all indexer watermarks and progress markers.  We do NOT
    # wipe the Phase 3a triage watermark (history_id) — that's a
    # separate concern.
    for key in (
        "index_phase1_watermark",
        "index_phase2_watermark",
        "index_incremental_watermark",
        "index_phase1_completed_at",
        "index_phase2_completed_at",
        "index_running",
        "index_paused",
        "index_last_batch_id",
        "index_last_phase",
    ):
        try:
            pkg_state.delete(key)
        except Exception:
            pass
    try:
        pkg_state.set(
            "index_last_reindex",
            json.dumps({"reason": reason, "vectors_cleared": int(cleared)}),
        )
    except Exception:
        pass
    return json.dumps({
        "ok": True,
        "vectors_cleared": int(cleared),
        "note": (
            "Indexer watermarks reset.  The Phase 1 / Phase 2 / "
            "incremental triggers will resume on their next tick."
        ),
    })


@chat_tool(
    description=(
        "Pause all three email indexer triggers (Phase 1, Phase 2, "
        "incremental).  Useful when the user is hitting their Gmail "
        "API quota or wants to suspend background work.  Does not "
        "cancel in-flight arcs; new arcs simply will not spawn until "
        "pkg_gmail_reindex_resume is called.  Requires user confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short free-text reason (<= 256 chars).",
                "maxLength": 256,
            },
        },
        "required": [],
    },
    requires_user_confirm=True,
    capabilities=[],
)
def pkg_gmail_reindex_pause(tool_input, **kwargs):
    """Set the indexer pause flag in package_state."""
    reason = (tool_input.get("reason") or "").strip()[:256]
    try:
        from carpenter.packages.state import PackageStateHandle
    except ImportError:
        return json.dumps({"error": "package.state unavailable"})
    pkg_state = PackageStateHandle("carpenter-gmail")
    try:
        pkg_state.set(
            "index_paused",
            json.dumps({"paused": True, "reason": reason}),
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"pause failed: {exc}"})
    return json.dumps({
        "ok": True,
        "note": "Indexer paused.  Run pkg_gmail_reindex_resume to resume.",
    })


@chat_tool(
    description=(
        "Resume the email indexer triggers after a pause.  Clears the "
        "package_state pause flag; the next 60-second heartbeat will "
        "pick up where the indexer left off (watermark-based, no work "
        "is lost across pauses).  Requires user confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    requires_user_confirm=True,
    capabilities=[],
)
def pkg_gmail_reindex_resume(tool_input, **kwargs):
    """Clear the indexer pause flag."""
    try:
        from carpenter.packages.state import PackageStateHandle
    except ImportError:
        return json.dumps({"error": "package.state unavailable"})
    pkg_state = PackageStateHandle("carpenter-gmail")
    try:
        pkg_state.delete("index_paused")
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"resume failed: {exc}"})
    return json.dumps({"ok": True, "note": "Indexer resumed."})
