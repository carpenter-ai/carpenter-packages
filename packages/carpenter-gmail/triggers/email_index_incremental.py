"""Incremental email-index trigger.

Walks Gmail's ``users.history.list`` from a stored ``historyId``
watermark and indexes any newly-added messages.  Separate from the
Phase 3a triage trigger (different watermark key, different batch
ceiling, different downstream pipeline).
"""

from __future__ import annotations

from typing import Any

from ._gmail_index_base import GmailIndexTriggerBase


_DEFAULT_MAX_BATCH = 25


class EmailIndexIncrementalTrigger(GmailIndexTriggerBase):
    """Indexes newly-arrived Gmail messages in batches of <=25.

    Manifest config keys:

    * ``cadence_seconds``: int, default 60 (floor 60).
    * ``max_batch``:        int 1-25, default 25.

    On first-run the watermark is empty — the trigger defers until
    the Phase 3a poller's ``gmail_account_email`` key is populated
    AND the operator manually seeds an incremental watermark (or
    Phase 1 has run far enough to provide one).  The trigger does
    NOT call ``users.getProfile`` itself; that responsibility lives
    with the Phase 3a poller.
    """

    phase = "incremental"
    template_name = "email_index_incremental"

    @classmethod
    def trigger_type(cls) -> str:
        return "email_index_incremental"

    def __init__(
        self,
        name: str,
        config: dict,
        *,
        source_package: str | None = None,
        package_state: Any = None,
        package_vectors: Any = None,
    ) -> None:
        super().__init__(
            name, config,
            source_package=source_package,
            package_state=package_state,
            package_vectors=package_vectors,
        )
        mb_raw = config.get("max_batch", _DEFAULT_MAX_BATCH)
        try:
            mb = int(mb_raw)
        except (TypeError, ValueError):
            mb = _DEFAULT_MAX_BATCH
        if mb < 1:
            mb = 1
        if mb > 25:
            mb = 25
        self.max_batch = mb

    def build_executor_seed(self, *, watermark_before: str) -> dict | None:
        if not watermark_before:
            # No starting historyId.  Try to bootstrap from the Phase
            # 3a poller's watermark (same Gmail ``historyId`` shape).
            try:
                from .gmail_poll import _KEY_HISTORY_ID
            except ImportError:
                return None
            try:
                seed = self.package_state.get(_KEY_HISTORY_ID)
            except Exception:
                return None
            if not seed:
                return None
            watermark_before = str(seed)
        return {
            "start_history_id": watermark_before,
        }
