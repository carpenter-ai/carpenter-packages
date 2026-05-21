"""Phase 1 (backfill, descending internalDate) email-index trigger.

See :mod:`carpenter_email.triggers._index_common` for the shared
lifecycle; this module only picks the phase-specific knobs and the
EXECUTOR pre-seed shape.
"""

from __future__ import annotations

from typing import Any

from ._index_common import IndexTriggerBase


_DEFAULT_MAX_BATCH = 100  # JUDGE-enforced ceiling per data_models


class EmailIndexPhase1Trigger(IndexTriggerBase):
    """Polls Gmail for messages older than the current Phase-1
    watermark in batches of <=100.

    Manifest config keys:

    * ``cadence_seconds``: int, default 60 (floor 60).
    * ``max_batch``:        int 1-100, default 100.
    """

    phase = "1"
    template_name = "email_index_phase1"

    @classmethod
    def trigger_type(cls) -> str:
        return "email_index_phase1"

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
        if mb > 100:
            mb = 100
        self.max_batch = mb

    def build_executor_seed(self, *, watermark_before: str) -> dict | None:
        return {
            "watermark_before": watermark_before,
            "max_batch": self.max_batch,
        }
