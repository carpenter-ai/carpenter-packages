"""Subscription handler: spawn the ``email_triage`` arc tree per event.

Wiring (Phase 3a PR-C):

* The backend's poll trigger (e.g. ``GmailPollTrigger``) emits
  ``email.received`` events with a minimal payload
  (``provider_message_id``, ``received_history_id``, ``account``).
* The manifest's ``trigger_subscriptions`` routes each event to
  :func:`handle_email_received`.
* This handler spawns one ``email_triage`` arc tree (PLANNER ->
  EXECUTOR -> REVIEWER -> JUDGE), pre-seeding the EXECUTOR arc state
  with the provider_message_id so the package's fetch script picks
  the right message.

The actual arc-tree shape mirrors the read arc tree in
``arc_builders`` — see that helper for the security rationale
(raw_email Resource is untrusted; briefing Resource is born-trusted
via PLANNER; extract Resource is pending until the JUDGE flips its
verdict).

Handlers run on the work queue (the subscription action enqueues a
``package.dispatch`` work item); they receive the event payload as a
plain dict and return ``None``.  Failures are logged but do not
propagate — a single broken event must not block the rest of the
queue.
"""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


def handle_email_received(payload: dict[str, Any]) -> None:
    """Spawn an ``email_triage`` arc tree for one inbound message.

    Args:
        payload: The original event payload from the backend's poll
            trigger ``emit``.  Expected keys: ``provider_message_id``,
            ``received_history_id``, ``account``.

    The handler is intentionally narrow: it does NOT fetch the
    message, does NOT classify, does NOT touch chat state.  Its only
    job is to spawn the triage pipeline; everything else flows
    through the standard arc completion → chat-notify path.
    """
    mid = payload.get("provider_message_id")
    if not isinstance(mid, str) or not mid:
        logger.warning(
            "handle_email_received: payload missing provider_message_id: %r",
            payload,
        )
        return
    account = payload.get("account") or ""
    history_id = payload.get("received_history_id") or ""
    try:
        # The owning package's ``tools`` module supplies a thin
        # ``_create_triage_arc_tree`` wrapper that binds the backend's
        # pre-verified fetch script + raw-source prefix and delegates
        # to the shared builder in ``arc_builders``.  The relative
        # import resolves under the platform's package namespace
        # (``_carpenter_pkg_.<package>.tools``) and under the test
        # harness, which loads the package via the same loader.
        from ..tools import _create_triage_arc_tree
    except ImportError:
        # Tools module not importable (would happen in a stripped test
        # build).  Log and bail — production never hits this branch.
        logger.exception(
            "handle_email_received: cannot import "
            "_create_triage_arc_tree; skipping message %s", mid,
        )
        return
    try:
        result = _create_triage_arc_tree(
            provider_message_id=mid,
            received_history_id=str(history_id),
            expected_account_email=str(account),
        )
    except Exception:
        logger.exception(
            "handle_email_received: arc-tree creation failed for "
            "provider_message_id=%s", mid,
        )
        return
    if isinstance(result, dict) and "error" in result:
        logger.warning(
            "handle_email_received: arc-tree creation rejected for "
            "provider_message_id=%s: %s",
            mid, result.get("error"),
        )
        return
    logger.info(
        "handle_email_received: spawned email_triage arc %s for "
        "provider_message_id=%s (account=%s)",
        result.get("arc_id") if isinstance(result, dict) else "?",
        mid, account or "<unknown>",
    )
