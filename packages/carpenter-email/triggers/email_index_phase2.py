"""Phase 2 (body re-index for a pre-seeded message-id list) trigger.

In v0.6.0 the Phase 2 EXECUTOR script still uses
Gmail ``format=metadata`` (no body harvest yet), so this phase is a
no-op until a future version lifts the body harvesting.  We ship the
trigger anyway so the manifest's plumbing is in place; the trigger's
:meth:`build_executor_seed` returns ``None`` whenever there are no
candidate ids and the base class skips the tick.
"""

from __future__ import annotations

import json
from typing import Any

from ._index_common import IndexTriggerBase


_DEFAULT_MAX_BATCH = 50


class EmailIndexPhase2Trigger(IndexTriggerBase):
    """Re-indexes message bodies for a list of message ids stored
    under ``index_phase2_candidates`` in :mod:`package_state`.

    The candidate list is populated by a future selection job (out
    of scope for Phase 4 PR-A); until populated, this trigger is a
    no-op.

    Manifest config keys:

    * ``cadence_seconds``: int, default 60 (floor 60).
    * ``max_batch``:        int 1-50, default 50.
    """

    phase = "2"
    template_name = "email_index_phase2"

    @classmethod
    def trigger_type(cls) -> str:
        return "email_index_phase2"

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
        if mb > 50:
            mb = 50
        self.max_batch = mb

    def build_executor_seed(self, *, watermark_before: str) -> dict | None:
        try:
            raw = self.package_state.get("index_phase2_candidates")
        except Exception:
            return None
        if not raw:
            return None
        try:
            ids = json.loads(raw)
        except Exception:
            return None
        if not isinstance(ids, list) or not ids:
            return None
        # Take the head; the trigger will pop the consumed ids after
        # a successful tick.  (Pop-after-consume is left to a future
        # PR; in v0.6.0 the trigger consumes from the top and relies
        # on the future selection job to refresh the list.)
        head = [str(x) for x in ids if isinstance(x, str)][:self.max_batch]
        if not head:
            return None
        return {
            "message_ids_json": json.dumps(head),
        }
