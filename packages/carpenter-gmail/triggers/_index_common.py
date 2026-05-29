"""Shared logic for the three email-index PollableTriggers (Phase 4).

The triggers are thin: each one fixes a phase and per-tick max-batch
cap, then defers to :class:`IndexTriggerBase` for everything else
(cadence guard, pause guard, mutual-exclusion lock, in-flight drain,
embed+upsert in trusted context, watermark CAS, receipt write).

Design properties (cross-reference ``phase-4-plan.md`` D5, D8, D12):

* **One arc per tick max**.  The trigger uses ``index_running`` as a
  CAS-protected mutex shared by all three indexer triggers.  If the
  lock is held by another phase's in-flight arc the trigger noops.
* **Trusted-context vector writes**.  Embed + upsert happens AFTER
  the JUDGE-graduates the ``EmailIndexFetchedBatch`` extract — the
  EXECUTOR never touches the vector store.  This closes the D24 I3
  hole (untrusted → trusted via JUDGE only).
* **No core changes**.  Everything uses already-shipped
  ``package_state`` / ``package_vectors`` injection.
* **Per-package E1 invariant**.  Vector floats never appear in any
  trusted-context string.  The trigger reads scores back from
  ``embed_and_search`` but only at chat-tool surface in
  :mod:`carpenter_gmail.tools` — this module deals only in embed
  inputs and upsert ids/metadata.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from carpenter.core.engine.triggers.base import PollableTrigger


logger = logging.getLogger(__name__)


# Shared package_state keys.  Held under the carpenter-gmail
# PackageStateHandle (auto-namespaced; cannot collide with another
# package).
KEY_PAUSED = "index_paused"
KEY_RUNNING = "index_running"
KEY_LAST_BATCH_ID = "index_last_batch_id"
KEY_LAST_PHASE = "index_last_phase"

# Per-phase watermark keys.  Phase 1 advances by oldest internalDate;
# Phase 2 advances by message-id-list completion; incremental
# advances by Gmail historyId.
KEY_WATERMARK_BY_PHASE = {
    "1":           "index_phase1_watermark",
    "2":           "index_phase2_watermark",
    "incremental": "index_incremental_watermark",
}
KEY_PHASE1_COMPLETED_AT = "index_phase1_completed_at"
KEY_PHASE2_COMPLETED_AT = "index_phase2_completed_at"

# Per-instance in-flight tracking — stored under
# ``index_inflight_<phase>``.  Holds a JSON blob {"arc_id":int,
# "resource_id":int, "started_at":iso, "watermark_before":str,
# "batch_id":str} so a daemon restart can resume drainage.
def inflight_key(phase: str) -> str:
    return f"index_inflight_{phase}"


# Tunables.
DEFAULT_CADENCE_SECONDS = 60
RUNNING_LOCK_STALE_SECONDS = 10 * 60   # CAS lock counts as stale after 10 min
INFLIGHT_TIMEOUT_SECONDS = 30 * 60     # cancel in-flight arcs after 30 min


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_iso_or_none(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def make_batch_id() -> str:
    """Generate a fresh JUDGE-shape-compatible batch id.

    Shape: ``^[a-zA-Z0-9_-]{5,64}$``.  We use ``<unix_seconds>_<rand>``
    so log scans by epoch work and collision risk is negligible.
    """
    ts = int(time.time())
    return f"{ts}_{secrets.token_urlsafe(8)}".replace("=", "")[:48]


class IndexTriggerBase(PollableTrigger):
    """Common base for all three carpenter-gmail index triggers.

    Subclasses provide:

    * :attr:`phase` — one of ``"1"`` / ``"2"`` / ``"incremental"``.
    * :attr:`max_batch` — per-tick fetch cap.
    * :attr:`template_name` — manifest arc template to spawn.
    * :meth:`build_executor_seed` — phase-specific arc-state keys.

    The base class handles cadence, pause, mutex, drain, embed+upsert,
    receipt write, and watermark CAS.
    """

    phase: str = ""           # subclass override
    max_batch: int = 25        # subclass override
    template_name: str = ""    # subclass override
    event_type_status: str = "email.index.status"

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
        # Cadence is config-overridable, floored at 60s.
        cadence_raw = config.get("cadence_seconds", DEFAULT_CADENCE_SECONDS)
        try:
            cadence = int(cadence_raw)
        except (TypeError, ValueError):
            cadence = DEFAULT_CADENCE_SECONDS
        if cadence < 60:
            cadence = 60
        self.cadence_seconds = cadence
        self._last_check_at: datetime | None = None

    # ── PollableTrigger contract ─────────────────────────────────────

    def check(self) -> None:
        """Heartbeat entry."""
        if self.package_state is None:
            return
        if self.package_vectors is None:
            return
        now = now_utc()
        # Per-instance cadence gate.
        if self._last_check_at is not None:
            delta = (now - self._last_check_at).total_seconds()
            if delta < self.cadence_seconds:
                return
        self._last_check_at = now

        # Pause guard.
        if self._is_paused():
            logger.debug(
                "%s: indexer paused; skipping tick", self.name,
            )
            return

        # 1) Drain any in-flight arc: if its extract Resource has
        #    been JUDGE-approved we embed+upsert here, advance the
        #    watermark, write a receipt, and clear the flag.  If
        #    the arc is still running we noop.  If the arc failed
        #    we clear the flag and back off this tick.
        drained = self._drain_inflight()
        if drained == "still_running":
            return  # next heartbeat will check again

        # 2) Spawn a new tick if we can grab the lock and the phase
        #    has not finished.
        if self._is_phase_finished():
            return
        if not self._claim_running_lock():
            logger.debug(
                "%s: another indexer holds the lock; skipping",
                self.name,
            )
            return
        try:
            self._spawn_tick()
        except Exception:
            logger.exception(
                "%s: spawn_tick failed", self.name,
            )
            self._release_running_lock()

    # ── Pause / mutex ────────────────────────────────────────────────

    def _is_paused(self) -> bool:
        try:
            raw = self.package_state.get(KEY_PAUSED)
        except Exception:
            return False
        return bool(raw)

    def _claim_running_lock(self) -> bool:
        """CAS-acquire the shared mutex with stale-detection.

        Stored value: JSON ``{"phase":..., "name":..., "since":iso}``.
        A lock older than :data:`RUNNING_LOCK_STALE_SECONDS` is
        considered crashed and we forcibly replace it.
        """
        state = self.package_state
        try:
            res = state.get_with_version(KEY_RUNNING)
        except Exception:
            logger.exception("%s: read running lock failed", self.name)
            return False
        new_blob = json.dumps({
            "phase": self.phase,
            "name": self.name,
            "since": iso(now_utc()),
        })
        if res is None:
            # No row → CAS from version 0.
            try:
                return bool(state.cas(KEY_RUNNING, 0, new_blob))
            except Exception:
                logger.exception("%s: cas running lock (new) failed", self.name)
                return False
        current_value, version = res
        # Existing lock — only steal if stale.
        try:
            data = json.loads(current_value)
            since = parse_iso_or_none(data.get("since"))
        except Exception:
            since = None
        if since is None:
            return False
        if (now_utc() - since).total_seconds() < RUNNING_LOCK_STALE_SECONDS:
            return False
        # Stale — try to steal via CAS.
        try:
            return bool(state.cas(KEY_RUNNING, int(version), new_blob))
        except Exception:
            logger.exception("%s: cas running lock (steal) failed", self.name)
            return False

    def _release_running_lock(self) -> None:
        try:
            self.package_state.delete(KEY_RUNNING)
        except Exception:
            logger.debug("%s: release running lock failed", self.name, exc_info=True)

    # ── In-flight drain ──────────────────────────────────────────────

    def _drain_inflight(self) -> str:
        """Drive any in-flight tick to completion.

        Returns one of:
          * ``"none"`` — nothing in-flight.
          * ``"still_running"`` — extract not yet approved or rejected.
          * ``"completed"`` — embed + upsert + watermark + receipt done.
          * ``"failed"`` — extract was rejected or arc failed.
        """
        state = self.package_state
        key = inflight_key(self.phase)
        try:
            blob_raw = state.get(key)
        except Exception:
            return "none"
        if not blob_raw:
            return "none"
        try:
            blob = json.loads(blob_raw)
            arc_id = int(blob.get("arc_id") or 0)
            resource_id = int(blob.get("resource_id") or 0)
            batch_id = str(blob.get("batch_id") or "")
            watermark_before = str(blob.get("watermark_before") or "")
            started_at = parse_iso_or_none(blob.get("started_at"))
        except Exception:
            logger.warning(
                "%s: malformed in-flight blob %r; clearing",
                self.name, blob_raw,
            )
            self._clear_inflight()
            return "none"
        if arc_id == 0 or resource_id == 0:
            self._clear_inflight()
            return "none"

        # Timeout check.
        if started_at is not None:
            age = (now_utc() - started_at).total_seconds()
            if age > INFLIGHT_TIMEOUT_SECONDS:
                logger.warning(
                    "%s: in-flight arc %d aged out (%.0fs); clearing",
                    self.name, arc_id, age,
                )
                self._clear_inflight()
                self._release_running_lock()
                return "failed"

        # Look up the extract Resource's current verdict.
        verdict = self._resource_verdict(resource_id)
        if verdict == "pending":
            return "still_running"
        if verdict == "rejected":
            logger.warning(
                "%s: extract resource %d rejected by JUDGE; clearing tick",
                self.name, resource_id,
            )
            self._clear_inflight()
            self._release_running_lock()
            return "failed"
        if verdict != "approved":
            # Unknown / missing — back off and retry next tick.
            return "still_running"

        # Approved — embed + upsert + advance watermark + write receipt.
        try:
            self._consume_approved_batch(
                resource_id=resource_id,
                batch_id=batch_id,
                watermark_before=watermark_before,
            )
        except Exception:
            logger.exception(
                "%s: consuming approved batch failed (resource_id=%d)",
                self.name, resource_id,
            )
            self._clear_inflight()
            self._release_running_lock()
            return "failed"
        self._clear_inflight()
        self._release_running_lock()
        return "completed"

    def _resource_verdict(self, resource_id: int) -> str:
        """Return the current ``template_verdict`` on a Resource id."""
        try:
            from carpenter.db import db_transaction as _db_transaction
        except ImportError:
            return ""
        try:
            with _db_transaction() as db:
                row = db.execute(
                    "SELECT template_verdict FROM resources WHERE id = ?",
                    (resource_id,),
                ).fetchone()
        except Exception:
            return ""
        if row is None:
            return ""
        v = row["template_verdict"] if hasattr(row, "keys") else row[0]
        return (v or "").strip()

    def _consume_approved_batch(
        self,
        *,
        resource_id: int,
        batch_id: str,
        watermark_before: str,
    ) -> None:
        """Embed each entry, upsert into the package vector store,
        advance the per-phase watermark, and write an audit receipt.
        """
        from carpenter.core.resources import (
            read_resource_content as _read_resource_content,
        )
        try:
            from carpenter_gmail.data_models import (
                EmailIndexFetchedBatch,
                EMAIL_INDEX_MAX_BATCH,
            )
        except ImportError:
            from .data_models import (  # type: ignore
                EmailIndexFetchedBatch,
                EMAIL_INDEX_MAX_BATCH,
            )

        # Load the JUDGE-graduated extract.  ``read_resource_content``
        # returns the file text; deserialise the dataclass via the
        # platform's JUDGE-dispatch deserialiser if available, else
        # direct JSON.  ``caller_arc_id=None`` is the trigger-context
        # form — we are not an arc, but a trusted in-process worker.
        raw = _read_resource_content(resource_id, caller_arc_id=None)
        batch = self._deserialise_batch(raw, EmailIndexFetchedBatch)
        if not isinstance(batch, EmailIndexFetchedBatch):
            raise RuntimeError(
                f"resource {resource_id} did not deserialise into "
                f"EmailIndexFetchedBatch: got {type(batch).__name__}"
            )
        if batch.batch_id != batch_id:
            # Catastrophic: the in-flight blob's id doesn't match the
            # extract's id.  Refuse to consume.
            raise RuntimeError(
                f"batch_id mismatch: in-flight={batch_id!r}, "
                f"extract={batch.batch_id!r}"
            )

        # Pause-marker path (model-identity mismatch or history-expired).
        if batch.error_kind:
            self._handle_error_kind(batch, watermark_before)
            return

        # Per-entry embed + upsert.  E1: only validated fields enter
        # the embed text; no raw script JSON, no vector floats in any
        # trusted string.
        embedded = 0
        errors = 0
        sample_error_message = ""
        cap = min(len(batch.entries), EMAIL_INDEX_MAX_BATCH)
        for entry in batch.entries[:cap]:
            try:
                embed_text = self._compose_embed_text(entry)
                metadata = self._entry_metadata(entry, batch.phase)
                self.package_vectors.embed_and_upsert(
                    id=entry.provider_message_id,
                    text=embed_text,
                    metadata=metadata,
                )
                embedded += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if not sample_error_message:
                    # Strip control chars and clamp to 512 chars to
                    # satisfy judge_email_index_batch.
                    msg = str(exc).replace("\n", " ").replace("\r", " ")
                    msg = "".join(ch for ch in msg if 0x20 <= ord(ch) < 0x7F)
                    sample_error_message = msg[:512]
                logger.exception(
                    "%s: embed_and_upsert failed for %s",
                    self.name, entry.provider_message_id,
                )

        # Advance the watermark via CAS.  If the CAS race-loses to a
        # concurrent re-init, we drop our update silently — the same
        # batch is idempotent on re-run.
        new_watermark = batch.watermark_after
        if new_watermark:
            self._cas_watermark(new_watermark)

        # Write an audit receipt to package_state so the chat agent
        # can introspect indexing progress.
        try:
            from carpenter_gmail.data_models import EmailIndexBatchReceipt
        except ImportError:
            from .data_models import EmailIndexBatchReceipt  # type: ignore
        receipt = EmailIndexBatchReceipt(
            phase=batch.phase,
            batch_id=batch.batch_id,
            watermark_before=batch.watermark_before,
            watermark_after=batch.watermark_after,
            embedded_count=embedded,
            error_count=errors,
            sample_error_message=sample_error_message,
            schema_version="1.0",
        )
        try:
            self.package_state.set(
                KEY_LAST_BATCH_ID, batch.batch_id,
            )
            self.package_state.set(KEY_LAST_PHASE, batch.phase)
            # Receipt itself stored as JSON for chat surfacing.
            self.package_state.set(
                f"index_last_receipt_{batch.phase}",
                json.dumps({
                    "phase": receipt.phase,
                    "batch_id": receipt.batch_id,
                    "watermark_before": receipt.watermark_before,
                    "watermark_after": receipt.watermark_after,
                    "embedded_count": receipt.embedded_count,
                    "error_count": receipt.error_count,
                    "sample_error_message": receipt.sample_error_message,
                }),
            )
        except Exception:
            logger.exception(
                "%s: writing receipt to package_state failed",
                self.name,
            )
        # Phase-finish detection.
        if embedded == 0 and errors == 0 and not batch.entries:
            self._mark_phase_finished()

    def _deserialise_batch(self, raw: Any, klass) -> Any:
        """Reconstruct an EmailIndexFetchedBatch from the JSON text on
        disk.  Mirrors the platform's :func:`_load_extraction_resource`
        kind-based dispatch (kind -> ``cls(**payload)``) but tolerates
        the nested ``entries`` tuple-of-dataclass shape that the
        platform handles for kind-less Resources via a recursive
        ``from_dict`` style.
        """
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            data = json.loads(raw)
        elif isinstance(raw, dict):
            data = raw
        else:
            raise RuntimeError(
                f"cannot deserialise resource bytes of type {type(raw).__name__}",
            )
        from dataclasses import fields, is_dataclass
        if not is_dataclass(klass):
            raise RuntimeError(f"{klass!r} is not a dataclass")
        try:
            from carpenter_gmail.data_models import EmailIndexFetchedEntry
        except ImportError:
            from .data_models import EmailIndexFetchedEntry  # type: ignore
        entries_raw = data.get("entries") or ()
        entry_fields = {f.name for f in fields(EmailIndexFetchedEntry)}
        entries = tuple(
            EmailIndexFetchedEntry(**{
                k: (tuple(v) if isinstance(v, list) else v)
                for k, v in e.items()
                if k in entry_fields
            })
            for e in entries_raw
        )
        klass_fields = {f.name for f in fields(klass)}
        clean = {k: data.get(k) for k in klass_fields if k != "entries"}
        clean["entries"] = entries
        return klass(**clean)

    def _compose_embed_text(self, entry: Any) -> str:
        """Build the natural-language embedding input for one entry.

        Concatenates the JUDGE-validated metadata fields.  NEVER
        includes vector floats, NEVER includes raw script output
        outside the dataclass.
        """
        parts = []
        if entry.from_display_clean:
            parts.append(f"From: {entry.from_display_clean}")
        if entry.from_address:
            parts.append(f"<{entry.from_address}>")
        if entry.subject_raw:
            parts.append(f"Subject: {entry.subject_raw}")
        if entry.gmail_snippet:
            parts.append(entry.gmail_snippet)
        if entry.body_text_or_null:
            parts.append(entry.body_text_or_null)
        text = " | ".join(parts)
        return text[:8000]

    def _entry_metadata(self, entry: Any, phase: str) -> dict:
        """Build the upsert metadata dict.  Only JUDGE-validated
        fields, no floats, no raw script output.
        """
        return {
            "phase": phase,
            "provider_message_id": entry.provider_message_id,
            "thread_id": entry.thread_id,
            "from_address": entry.from_address,
            "date_iso": entry.date_iso,
            "has_attachment": entry.has_attachment,
            "labels": list(entry.labels),
        }

    def _cas_watermark(self, new_watermark: str) -> None:
        key = KEY_WATERMARK_BY_PHASE.get(self.phase)
        if not key:
            return
        state = self.package_state
        try:
            res = state.get_with_version(key)
        except Exception:
            return
        if res is None:
            try:
                state.cas(key, 0, new_watermark)
            except Exception:
                logger.exception("%s: cas new watermark failed", self.name)
            return
        _current_value, version = res
        try:
            state.cas(key, int(version), new_watermark)
        except Exception:
            logger.exception("%s: cas advance watermark failed", self.name)

    def _handle_error_kind(self, batch, watermark_before: str) -> None:
        """Handle a non-empty error_kind on a JUDGE-approved batch.

        ``"model_identity_mismatch"`` — pause indexing; the user must
        run pkg_gmail_reindex to clear the namespace and restart.

        ``"history_expired"`` — clear the incremental watermark so
        Phase 1 backfill can take over for the missed range.
        """
        if batch.error_kind == "model_identity_mismatch":
            try:
                self.package_state.set(
                    KEY_PAUSED,
                    json.dumps({
                        "paused": True,
                        "reason": (
                            "model_identity changed since last index "
                            "batch; run pkg_gmail_reindex to clear "
                            "the namespace and restart from scratch"
                        ),
                    }),
                )
            except Exception:
                logger.exception(
                    "%s: setting pause flag failed", self.name,
                )
        elif batch.error_kind == "history_expired":
            try:
                self.package_state.delete(
                    KEY_WATERMARK_BY_PHASE.get("incremental"),
                )
            except Exception:
                logger.exception(
                    "%s: clearing incremental watermark failed",
                    self.name,
                )
        else:
            # JUDGE should have rejected anything else — log and ignore.
            logger.warning(
                "%s: unexpected error_kind %r on approved batch",
                self.name, batch.error_kind,
            )

    def _is_phase_finished(self) -> bool:
        """Phase 1 / 2 have a completion timestamp; incremental never."""
        if self.phase == "1":
            try:
                return bool(self.package_state.get(KEY_PHASE1_COMPLETED_AT))
            except Exception:
                return False
        if self.phase == "2":
            try:
                return bool(self.package_state.get(KEY_PHASE2_COMPLETED_AT))
            except Exception:
                return False
        return False

    def _mark_phase_finished(self) -> None:
        if self.phase == "1":
            try:
                self.package_state.set(KEY_PHASE1_COMPLETED_AT, iso(now_utc()))
            except Exception:
                logger.exception("%s: marking phase 1 done failed", self.name)
        elif self.phase == "2":
            try:
                self.package_state.set(KEY_PHASE2_COMPLETED_AT, iso(now_utc()))
            except Exception:
                logger.exception("%s: marking phase 2 done failed", self.name)

    def _clear_inflight(self) -> None:
        try:
            self.package_state.delete(inflight_key(self.phase))
        except Exception:
            logger.debug(
                "%s: clearing in-flight blob failed",
                self.name, exc_info=True,
            )

    def _set_inflight(
        self, arc_id: int, resource_id: int,
        batch_id: str, watermark_before: str,
    ) -> None:
        blob = json.dumps({
            "arc_id": int(arc_id),
            "resource_id": int(resource_id),
            "batch_id": batch_id,
            "watermark_before": watermark_before,
            "started_at": iso(now_utc()),
        })
        try:
            self.package_state.set(inflight_key(self.phase), blob)
        except Exception:
            logger.exception(
                "%s: storing in-flight blob failed", self.name,
            )

    # ── Spawn entry point ────────────────────────────────────────────

    def _spawn_tick(self) -> None:
        """Spawn one arc tree for this phase.  Subclasses override
        :meth:`build_executor_seed` to provide phase-specific inputs.
        """
        # Read the expected-account email (cached by gmail_poll under
        # KEY_ACCOUNT_EMAIL, or empty if unauthorised yet).
        try:
            from carpenter_gmail.triggers.gmail_poll import _KEY_ACCOUNT_EMAIL
        except ImportError:
            from .gmail_poll import _KEY_ACCOUNT_EMAIL  # type: ignore
        try:
            account = self.package_state.get(_KEY_ACCOUNT_EMAIL) or ""
        except Exception:
            account = ""
        if not account:
            logger.debug(
                "%s: no GMAIL_OAUTH_ACCESS_TOKEN account on record yet; "
                "deferring",
                self.name,
            )
            self._release_running_lock()
            return

        # Read current model identity.  If embeddings are unavailable
        # in this build, defer (the operator hasn't configured the
        # embedding service yet).
        try:
            from carpenter.embeddings.service import get_embedding_service
            service = get_embedding_service()
            model_identity = service.model_identity
        except Exception:
            logger.debug(
                "%s: embedding service unavailable; deferring",
                self.name, exc_info=True,
            )
            self._release_running_lock()
            return

        watermark = self._read_watermark()
        seed = self.build_executor_seed(watermark_before=watermark)
        if seed is None:
            # Phase has nothing to do this tick.
            self._release_running_lock()
            return

        batch_id = make_batch_id()

        try:
            from carpenter_gmail.tools import _create_index_arc_tree
        except ImportError:
            from .tools import _create_index_arc_tree  # type: ignore
        result = _create_index_arc_tree(
            template_name=self.template_name,
            phase=self.phase,
            batch_id=batch_id,
            watermark_before=watermark,
            expected_account_email=account,
            model_identity=model_identity,
            executor_state_seed=seed,
        )
        if "error" in result:
            logger.warning(
                "%s: arc-tree creation failed: %s",
                self.name, result["error"],
            )
            self._release_running_lock()
            return
        self._set_inflight(
            arc_id=int(result["arc_id"]),
            resource_id=int(result["extract_resource_id"]),
            batch_id=batch_id,
            watermark_before=watermark,
        )
        logger.info(
            "%s: spawned indexer arc %d (resource %d) for batch %s",
            self.name, result["arc_id"], result["extract_resource_id"],
            batch_id,
        )

    def _read_watermark(self) -> str:
        key = KEY_WATERMARK_BY_PHASE.get(self.phase)
        if not key:
            return ""
        try:
            v = self.package_state.get(key)
        except Exception:
            return ""
        return str(v or "")

    # Subclass hook.
    def build_executor_seed(self, *, watermark_before: str) -> dict | None:
        """Return phase-specific arc-state keys to pre-seed on the
        EXECUTOR.  ``None`` means there is nothing to do this tick
        (e.g. Phase 2 has no candidate ids).
        """
        raise NotImplementedError
