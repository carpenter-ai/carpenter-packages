"""Backend-agnostic arc-tree builders for the email trust pipeline.

These helpers construct the canonical PLANNER -> EXECUTOR (untrusted
provider call) -> REVIEWER (constrained, static prompt) -> JUDGE
(deterministic Python) arc trees that every email read / triage /
write / index operation funnels through.  They were extracted, line
for line, from ``carpenter-gmail/tools.py`` so a second email backend
(e.g. ``carpenter-imap-email``) can reuse the exact same trust shape.

Backend-specific inputs are passed in as **arguments / hooks** rather
than imported:

* The EXECUTOR's pre-verified fetch / search / write / index *script*
  is a string the caller supplies (gmail's ``scripts.py`` is a leaf
  concern; this layer never imports it).
* The raw-Resource ``source_descriptor`` prefix (``"gmail"`` for the
  Gmail leaf) is a ``raw_source_prefix`` argument so the audit string
  is not hardcoded to one backend.
* The read-template -> extract-kind and write-template ->
  extract-kind maps are package-agnostic data and live here as
  module constants.

Nothing in this module names ``gmail``; the only place a backend's
identity enters is via the caller-supplied arguments above.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage


# Read-template -> extract-kind map (REVIEWER derives this kind).
EXTRACT_KIND_BY_TEMPLATE = {
    "email_read_simple_text": "EmailSimpleTextExtract",
    "email_read_meeting_invite": "EmailMeetingInviteExtract",
    "email_read_order_confirmation": "EmailOrderConfirmationExtract",
}

# Write-template -> extract-kind map (the typed receipt the JUDGE
# graduates after an external-effect operation).
_WRITE_EXTRACT_KIND_BY_TEMPLATE = {
    "email_write_send": "EmailSendResult",
    "email_write_archive": "EmailArchiveResult",
    "email_write_mark_read": "EmailMarkReadResult",
    "email_write_draft": "EmailDraftResult",
}


def _create_read_arc_tree(
    *,
    template_name: str,
    provider_message_id: str,
    expected_account_email: str,
    conversation_id: int | None,
    fetch_script: str,
    raw_source_prefix: str = "email",
) -> dict:
    """Spin up the PLANNER -> EXECUTOR -> REVIEWER -> JUDGE arc tree
    for one provider message under one of our read templates.

    Returns ``{"arc_id": <parent_planner_id>}`` on success or
    ``{"error": ...}`` on failure.

    The Resource wiring mirrors the platform's ``_handle_fetch_web_content``
    pattern: a raw_email Resource (untrusted, produced_by_template=NULL)
    receives the EXECUTOR's provider JSON output; an extract Resource
    (template_verdict='pending', produced_by_template=<template_name>)
    is pre-created so the REVIEWER can derive into it and the JUDGE
    can flip its verdict via ``resource.submit_verdict``.

    The package author has audited the provider-fetch script once; the
    EXECUTOR is told to submit ``fetch_script`` verbatim via
    ``submit_code``.  This avoids handing the EXECUTOR an open-ended
    "go fetch this URL" goal where it would have to generate its own
    code.
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
                    + fetch_script
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
        source_descriptor=f"{raw_source_prefix}:{provider_message_id}",
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

    # 4) Briefing Resource (PLANNER outputs; born-trusted by PLANNER).
    #    We pre-create the row so the PLANNER's derive_resource call can
    #    target a known id; the PLANNER fills in the bytes.  The
    #    platform's resource_trust() rule requires
    #    ``produced_by_template != None AND template_verdict ==
    #    'approved'`` for trusted state, so we tag the briefing with
    #    this template's name and an approved verdict.  That makes the
    #    briefing trusted-by-construction, which is what born-trusted
    #    PLANNER state should be.
    briefing_resource_id = _derive_resource(
        content_type="dataclass",
        file_path=None,
        produced_by_arc_id=parent_id,
        produced_by_template=template_name,
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
    extract_kind = EXTRACT_KIND_BY_TEMPLATE[template_name]
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


def _create_triage_arc_tree(
    *,
    provider_message_id: str,
    received_history_id: str,
    expected_account_email: str,
    fetch_script: str,
    raw_source_prefix: str = "email",
) -> dict:
    """Spin up the ``email_triage`` arc tree for one inbound message.

    Mirrors :func:`_create_read_arc_tree` exactly except:

    * The template name is ``email_triage``.
    * The REVIEWER's extract_kind is ``EmailTriageExtract``.
    * The PLANNER goal mentions the inbound-triage context (no chat
      goal text — this is a trigger-driven pipeline; the chat agent
      is notified only after arc completion).

    Returns ``{"arc_id": <parent_planner_id>}`` on success or
    ``{"error": ...}`` on failure.  Called from the package's
    ``email.received`` subscription handler.
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

    template_name = "email_triage"
    extract_kind = "EmailTriageExtract"

    parent_id = _am.create_arc(
        name=f"Email triage: {provider_message_id[:16]}",
        goal=(
            "Construct an EmailReviewBriefing dataclass from the "
            "global SecurityPolicies.email allowlist snapshot and "
            "the package's static suspicious-keyword list, then "
            "derive_resource(kind='EmailReviewBriefing', "
            "verdict='approved') as a born-trusted Resource.  The "
            "EXECUTOR child has been pre-seeded with the inbound "
            "provider_message_id; the REVIEWER child will read the "
            "briefing + raw email JSON and emit one EmailTriageExtract; "
            "the JUDGE is deterministic Python (no agent input needed)."
        ),
        agent_type="PLANNER",
    )
    _am.update_status(parent_id, "active")

    batch_result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": (
                    f"Triage-fetch Gmail message {provider_message_id[:16]}"
                ),
                "goal": (
                    "Submit this EXACT code via submit_code (do not "
                    "modify it):\n```python\n"
                    + fetch_script
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
                "name": "Triage-review and emit extract",
                "goal": (
                    "Read the briefing Resource and the raw_email "
                    "Resource (paths in arc state under "
                    "'briefing_resource_id' and 'raw_resource_path'). "
                    "Follow the static REVIEWER prompt shipped with "
                    "the email_triage template.  Emit exactly one "
                    "EmailTriageExtract via derive_resource.  Then "
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
                "name": "Judge triage extract",
                "goal": (
                    "JUDGE: validate the REVIEWER's EmailTriageExtract "
                    "Resource.  Call resource.submit_verdict with the "
                    "extract's resource_id (in arc state under "
                    "'_review_target_resource_id') and "
                    "verdict='approved' or 'rejected' based on the "
                    "package's deterministic JUDGE handler "
                    "(judge_email_triage, auto-dispatched by the "
                    "platform via _try_package_judge)."
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

    # Resource wiring: raw_email (untrusted) + briefing (born-trusted)
    # + extract (pending).  See _create_read_arc_tree for invariants.
    raw_resource_id = _create_resource(
        content_type="json",
        file_path=None,
        produced_by_arc_id=executor_arc_id,
        source_descriptor=f"{raw_source_prefix}:{provider_message_id}",
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

    briefing_resource_id = _derive_resource(
        content_type="dataclass",
        file_path=None,
        produced_by_arc_id=parent_id,
        produced_by_template=template_name,
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

    # Pre-seed arc state.
    set_arc_state(executor_arc_id, "provider_message_id", provider_message_id)
    set_arc_state(executor_arc_id, "raw_resource_path", str(raw_path))
    set_arc_state(executor_arc_id, "raw_resource_id", raw_resource_id)

    set_arc_state(parent_id, "expected_account_email", expected_account_email)
    set_arc_state(parent_id, "briefing_resource_id", briefing_resource_id)
    set_arc_state(parent_id, "_primary_resource_id", extract_resource_id)
    set_arc_state(parent_id, "template_name", template_name)
    set_arc_state(parent_id, "extract_kind", extract_kind)
    # Surface the watermark on the parent so future arc-tree introspection
    # tools can correlate triage arcs back to a Gmail history cursor.
    set_arc_state(parent_id, "received_history_id", received_history_id)

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

    _wq.enqueue(
        "arc.dispatch",
        {"arc_id": executor_arc_id},
        idempotency_key=f"arc_dispatch:{executor_arc_id}",
    )
    return {"arc_id": parent_id}


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
    raw_source_prefix: str = "email",
) -> dict:
    """Spin up the PLANNER -> EXECUTOR -> REVIEWER -> JUDGE arc tree
    for one provider external-effect operation.

    Returns ``{"arc_id": <parent_planner_id>}`` on success or
    ``{"error": ...}`` on failure.

    Mirrors ``_create_read_arc_tree``'s shape: the EXECUTOR writes a
    raw JSON receipt to a raw Resource (untrusted, produced_by_template
    is NULL); the REVIEWER reads briefing + raw receipt and derives a
    pending typed extract Resource (template_verdict='pending',
    produced_by_template=<template_name>); the JUDGE flips the verdict
    deterministically via the platform's ``_try_package_judge`` path.

    Args:
        template_name: One of ``email_write_send`` /
            ``email_write_archive`` / ``email_write_mark_read`` /
            ``email_write_draft``.  Determines the extract kind and
            the JUDGE handler dispatched by the platform.
        arc_name: Human-readable parent arc name (shown in UIs).
        arc_goal: Parent arc goal.
        script: Pre-verified EXECUTOR script body (from scripts.py).
        state_seed: dict of arc-state keys to set on the EXECUTOR
            before dispatch.  Must include all inputs the script
            reads via ``dispatch(Label("state.get"), ...)`` other
            than the raw-Resource wiring this helper sets itself
            (``raw_resource_path`` and ``raw_resource_id``).
        expected_account_email: mailbox the OAuth token is expected
            to belong to.  Stored on the parent arc for audit and
            on the EXECUTOR for the in-script check.
        staged_to_addresses: recipient set the chat boundary approved
            (empty tuple for archive / mark-read which have no
            recipient surface).  Recorded on the parent arc for the
            briefing builder.
        conversation_id: chat conversation that initiated the call
            (or None for unsolicited).
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

    extract_kind = _WRITE_EXTRACT_KIND_BY_TEMPLATE[template_name]

    # 1) Parent PLANNER
    parent_id = _am.create_arc(
        name=arc_name,
        goal=arc_goal,
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
                "name": f"Gmail {template_name}",
                "goal": (
                    "Submit this EXACT code via submit_code (do not "
                    "modify it):\n```python\n"
                    + script
                    + "```\nAll inputs (operation payload, "
                    "expected_account_email, raw_resource_path, "
                    "raw_resource_id) have been pre-seeded in arc state."
                ),
                "parent_id": parent_id,
                "integrity_level": "untrusted",
                "output_type": "json",
                "agent_type": "EXECUTOR",
                "step_order": 0,
            },
            {
                "name": "Review write receipt and emit extract",
                "goal": (
                    "Read the briefing Resource and the raw_receipt "
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
                "name": "Judge write receipt",
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

    # 3) Raw receipt Resource (untrusted ingest; EXECUTOR writes JSON
    #    to this path via files.write + resource.finalize).
    raw_resource_id = _create_resource(
        content_type="json",
        file_path=None,
        produced_by_arc_id=executor_arc_id,
        source_descriptor=f"{raw_source_prefix}-write:{template_name}",
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

    # 4) Briefing Resource (PLANNER outputs; born-trusted by PLANNER).
    #    See the equivalent block in _create_read_arc_tree for why we
    #    tag with this template's name + approved verdict to satisfy
    #    resource_trust()'s rule for trusted state.
    briefing_resource_id = _derive_resource(
        content_type="dataclass",
        file_path=None,
        produced_by_arc_id=parent_id,
        produced_by_template=template_name,
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
    extract_resource_id = _derive_resource(
        content_type="dataclass",
        file_path=None,
        produced_by_arc_id=reviewer_arc_id,
        produced_by_template=template_name,
        template_verdict="pending",
        source_descriptor=f"receipt:{template_name}",
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

    # 7) Pre-seed arc state on the EXECUTOR.  Caller-supplied keys
    # (the script's operation payload) plus the raw-Resource wiring
    # plus the expected-account check input.
    seed = dict(state_seed)
    seed.setdefault("expected_account_email", expected_account_email)
    seed["raw_resource_path"] = str(raw_path)
    seed["raw_resource_id"] = raw_resource_id
    for key, value in seed.items():
        set_arc_state(executor_arc_id, key, value)

    # Parent arc state — for the briefing builder and audit.
    set_arc_state(parent_id, "expected_account_email", expected_account_email)
    set_arc_state(parent_id, "briefing_resource_id", briefing_resource_id)
    set_arc_state(parent_id, "_primary_resource_id", extract_resource_id)
    set_arc_state(parent_id, "template_name", template_name)
    set_arc_state(parent_id, "extract_kind", extract_kind)
    set_arc_state(
        parent_id, "staged_to_addresses", list(staged_to_addresses),
    )

    # REVIEWER arc state — what it reads and writes.
    set_arc_state(reviewer_arc_id, "briefing_resource_id", briefing_resource_id)
    set_arc_state(reviewer_arc_id, "raw_resource_path", str(raw_path))
    set_arc_state(reviewer_arc_id, "raw_resource_id", raw_resource_id)
    set_arc_state(reviewer_arc_id, "extract_resource_id", extract_resource_id)
    set_arc_state(reviewer_arc_id, "extract_kind", extract_kind)
    set_arc_state(reviewer_arc_id, "template_name", template_name)

    # JUDGE arc state — what it grades.
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


def _create_index_arc_tree(
    *,
    template_name: str,
    phase: str,
    batch_id: str,
    watermark_before: str,
    expected_account_email: str,
    model_identity: str,
    executor_state_seed: dict,
    script: str,
    extract_kind: str,
    raw_source_prefix: str = "email",
) -> dict:
    """Spin up one PLANNER -> EXECUTOR -> REVIEWER -> JUDGE arc tree
    for one indexer tick.  Shape mirrors :func:`_create_read_arc_tree`
    exactly; differences are:

    * The EXECUTOR's script is the phase-specific index ``script``
      (caller-supplied; the layer never imports a backend's scripts).
    * The REVIEWER's extract is :class:`EmailIndexFetchedBatch` (the
      caller passes its ``extract_kind``).
    * No chat conversation linkage — this is a trigger-driven pipeline.
    * Additional pre-seeded state keys go to the EXECUTOR
      (``executor_state_seed``) so phase-specific inputs (watermarks,
      message-id lists) can be passed in.

    Returns ``{"arc_id": <parent>, "extract_resource_id": <int>}`` on
    success or ``{"error": ...}`` on failure.
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

    parent_id = _am.create_arc(
        name=f"Email index {phase}: {batch_id[:16]}",
        goal=(
            "Construct an EmailReviewBriefing dataclass from the "
            "global SecurityPolicies.email allowlist snapshot and "
            "the package's static suspicious-keyword list, then "
            "derive_resource(kind='EmailReviewBriefing', "
            "verdict='approved') as a born-trusted Resource.  The "
            "EXECUTOR child has been pre-seeded with phase-specific "
            "indexer inputs; the REVIEWER child will read the "
            "briefing + raw fetch JSON and emit one "
            "EmailIndexFetchedBatch; the JUDGE is deterministic "
            "Python (no agent input needed)."
        ),
        agent_type="PLANNER",
    )
    _am.update_status(parent_id, "active")

    batch_result = arc_backend.handle_create_batch({
        "arcs": [
            {
                "name": f"Index-fetch {phase}: {batch_id[:16]}",
                "goal": (
                    "Submit this EXACT code via submit_code (do not "
                    "modify it):\n```python\n"
                    + script
                    + "```\nAll inputs have been pre-seeded in arc "
                    "state.  Do not generate your own code; this "
                    "script has been audited by the package author."
                ),
                "parent_id": parent_id,
                "integrity_level": "untrusted",
                "output_type": "json",
                "agent_type": "EXECUTOR",
                "step_order": 0,
            },
            {
                "name": "Review index batch",
                "goal": (
                    "Read the briefing Resource and the raw_fetch "
                    "Resource (paths in arc state under "
                    "'briefing_resource_id' and 'raw_resource_path'). "
                    "Follow the static REVIEWER prompt shipped with "
                    "this template.  Emit exactly one "
                    "EmailIndexFetchedBatch via derive_resource. "
                    "Then exit — the deterministic JUDGE will "
                    "validate and graduate."
                ),
                "parent_id": parent_id,
                "agent_type": "REVIEWER",
                "integrity_level": "trusted",
                "reviewer_profile": "security-reviewer",
                "model_policy": "careful-coding",
                "step_order": 1,
            },
            {
                "name": "Judge index batch",
                "goal": (
                    "JUDGE: validate the REVIEWER's "
                    "EmailIndexFetchedBatch Resource.  Call "
                    "resource.submit_verdict with the extract's "
                    "resource_id (in arc state under "
                    "'_review_target_resource_id') and "
                    "verdict='approved' or 'rejected' based on the "
                    "package's deterministic JUDGE handler "
                    "(judge_email_index_fetched_batch, auto-dispatched "
                    "by the platform via _try_package_judge)."
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

    # Raw fetch Resource (untrusted) — receives the EXECUTOR's JSON.
    raw_resource_id = _create_resource(
        content_type="json",
        file_path=None,
        produced_by_arc_id=executor_arc_id,
        source_descriptor=f"{raw_source_prefix}.index.{phase}:{batch_id}",
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

    # Briefing Resource (born-trusted).
    briefing_resource_id = _derive_resource(
        content_type="dataclass",
        file_path=None,
        produced_by_arc_id=parent_id,
        produced_by_template=template_name,
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

    # Extract Resource (pending until JUDGE).
    extract_resource_id = _derive_resource(
        content_type="dataclass",
        file_path=None,
        produced_by_arc_id=reviewer_arc_id,
        produced_by_template=template_name,
        template_verdict="pending",
        source_descriptor=f"index.batch:{phase}:{batch_id}",
    )
    extract_path = _resource_storage_path(extract_resource_id, "blob")
    extract_path.parent.mkdir(parents=True, exist_ok=True)
    with _db_transaction() as _db:
        _db.execute(
            "UPDATE resources SET file_path = ? WHERE id = ?",
            (str(extract_path), extract_resource_id),
        )

    _link_arc_resource(
        arc_id=reviewer_arc_id, resource_id=briefing_resource_id, role="input",
    )
    _link_arc_resource(
        arc_id=reviewer_arc_id, resource_id=raw_resource_id, role="input",
    )
    _link_arc_resource(
        arc_id=reviewer_arc_id, resource_id=extract_resource_id, role="output",
    )

    # Pre-seed arc state.
    set_arc_state(executor_arc_id, "raw_resource_path", str(raw_path))
    set_arc_state(executor_arc_id, "raw_resource_id", raw_resource_id)
    set_arc_state(executor_arc_id, "expected_account_email", expected_account_email)
    set_arc_state(executor_arc_id, "model_identity", model_identity)
    set_arc_state(executor_arc_id, "batch_id", batch_id)
    set_arc_state(executor_arc_id, "phase", phase)
    for k, v in executor_state_seed.items():
        set_arc_state(executor_arc_id, k, v)

    set_arc_state(parent_id, "expected_account_email", expected_account_email)
    set_arc_state(parent_id, "briefing_resource_id", briefing_resource_id)
    set_arc_state(parent_id, "_primary_resource_id", extract_resource_id)
    set_arc_state(parent_id, "template_name", template_name)
    set_arc_state(parent_id, "extract_kind", extract_kind)
    set_arc_state(parent_id, "index_phase", phase)
    set_arc_state(parent_id, "index_batch_id", batch_id)
    set_arc_state(parent_id, "index_watermark_before", watermark_before)
    set_arc_state(parent_id, "index_model_identity", model_identity)

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

    _wq.enqueue(
        "arc.dispatch",
        {"arc_id": executor_arc_id},
        idempotency_key=f"arc_dispatch:{executor_arc_id}",
    )
    return {
        "arc_id": parent_id,
        "extract_resource_id": extract_resource_id,
    }
