"""
Package-internal: carpenter-gmail JUDGE handler golden cases.

For every JUDGE handler the package ships, this story constructs:

* One known-good extract and asserts the handler approves it.
* One known-bad extract (one field tweaked to violate the documented
  invariant) and asserts the handler rejects with a reason that cites
  the field.

The carpenter-linux platform stories (s055-s058) used to exercise the
JUDGE shape on every per-template arc tree; that's overkill — the JUDGE
is deterministic Python.  The platform stories now exercise the JUDGE
via the dispatch pipeline (with a representative extract); this story
owns the per-handler input/output contract.
"""

from __future__ import annotations

from ._framework import PackageStory, StoryResult, load_package_module


_GOOD_PROVIDER_ID = "abc123_xyz-456"
_GOOD_DRAFT_ID = "r-1234567890"
_GOOD_ACCOUNT = "ben@example.com"
_GOOD_RECIPIENT = "phase15-test-recipient@example.com"


class JudgeHandlersAcceptReject(PackageStory):
    name = "carpenter-gmail::judge_handlers_accept_reject"
    description = (
        "Every JUDGE handler approves a golden valid extract and "
        "rejects a golden malformed extract with a reason citing the "
        "violated field."
    )

    def run(self, client=None, db=None) -> StoryResult:
        dm = load_package_module("data_models")
        judges = load_package_module("judges")

        # carpenter_tools is a runtime dependency, importable via the
        # core path that ``ensure_carpenter_on_path`` set up.
        from carpenter_tools.policy.types import EmailPolicy

        # ── judge_email_send ───────────────────────────────────────
        self._case(
            judges.judge_email_send,
            good=dm.EmailSendResult(
                status="sent",
                expected_account_email=EmailPolicy(_GOOD_ACCOUNT),
                provider_message_id=_GOOD_PROVIDER_ID,
                to_addresses=(EmailPolicy(_GOOD_RECIPIENT),),
            ),
            bad=dm.EmailSendResult(
                status="not-sent",  # violates closed-enum
                expected_account_email=EmailPolicy(_GOOD_ACCOUNT),
                provider_message_id=_GOOD_PROVIDER_ID,
                to_addresses=(EmailPolicy(_GOOD_RECIPIENT),),
            ),
            bad_reason_contains="status",
            label="judge_email_send",
        )

        # ── judge_email_archive ────────────────────────────────────
        self._case(
            judges.judge_email_archive,
            good=dm.EmailArchiveResult(
                status="archived",
                expected_account_email=EmailPolicy(_GOOD_ACCOUNT),
                provider_message_id=_GOOD_PROVIDER_ID,
                was_already_archived=False,
            ),
            bad=dm.EmailArchiveResult(
                status="archived",
                expected_account_email=EmailPolicy(_GOOD_ACCOUNT),
                provider_message_id="x",  # too short for the regex
                was_already_archived=False,
            ),
            bad_reason_contains="provider_message_id",
            label="judge_email_archive",
        )

        # ── judge_email_mark_read ──────────────────────────────────
        self._case(
            judges.judge_email_mark_read,
            good=dm.EmailMarkReadResult(
                status="marked_read",
                expected_account_email=EmailPolicy(_GOOD_ACCOUNT),
                provider_message_id=_GOOD_PROVIDER_ID,
                was_already_read=True,
            ),
            bad=dm.EmailMarkReadResult(
                status="not-marked",  # violates closed-enum
                expected_account_email=EmailPolicy(_GOOD_ACCOUNT),
                provider_message_id=_GOOD_PROVIDER_ID,
                was_already_read=True,
            ),
            bad_reason_contains="status",
            label="judge_email_mark_read",
        )

        # ── judge_email_draft ──────────────────────────────────────
        self._case(
            judges.judge_email_draft,
            good=dm.EmailDraftResult(
                status="drafted",
                expected_account_email=EmailPolicy(_GOOD_ACCOUNT),
                provider_message_id=_GOOD_PROVIDER_ID,
                draft_id=_GOOD_DRAFT_ID,
                to_addresses=(EmailPolicy(_GOOD_RECIPIENT),),
            ),
            bad=dm.EmailDraftResult(
                status="drafted",
                expected_account_email=EmailPolicy(_GOOD_ACCOUNT),
                provider_message_id=_GOOD_PROVIDER_ID,
                draft_id="!!",  # malformed
                to_addresses=(EmailPolicy(_GOOD_RECIPIENT),),
            ),
            bad_reason_contains="draft_id",
            label="judge_email_draft",
        )

        # ── judge_email_triage ─────────────────────────────────────
        self._case(
            judges.judge_email_triage,
            good=dm.EmailTriageExtract(
                provider_message_id=_GOOD_PROVIDER_ID,
                received_history_id="1234567",
                category="personal",
                from_address=EmailPolicy("alice@example.com"),
                subject_clean="A clean subject",
                importance_flags=(),
            ),
            bad=dm.EmailTriageExtract(
                provider_message_id=_GOOD_PROVIDER_ID,
                received_history_id="1234567",
                category="not-a-category",  # not in closed enum
                from_address=EmailPolicy("alice@example.com"),
                subject_clean="A clean subject",
                importance_flags=(),
            ),
            bad_reason_contains="category",
            label="judge_email_triage",
        )

        # ── judge_simple_text ──────────────────────────────────────
        self._case(
            judges.judge_simple_text,
            good=dm.EmailSimpleTextExtract(
                provider_message_id=_GOOD_PROVIDER_ID,
                expected_account_email=EmailPolicy(_GOOD_ACCOUNT),
                from_address=EmailPolicy("alice@example.com"),
                to_addresses=(EmailPolicy(_GOOD_ACCOUNT),),
                subject="hello",
                received_at="2026-05-20T14:00:00Z",
                body_summary="A short, well-behaved body summary.",
                extracted_urls=(),
                flags=(),
                attachments=(),
            ),
            bad=dm.EmailSimpleTextExtract(
                provider_message_id=_GOOD_PROVIDER_ID,
                expected_account_email=EmailPolicy(_GOOD_ACCOUNT),
                from_address=EmailPolicy("alice@example.com"),
                to_addresses=(EmailPolicy(_GOOD_ACCOUNT),),
                subject="hello",
                received_at="not-a-date",  # malformed received_at
                body_summary="A short, well-behaved body summary.",
                extracted_urls=(),
                flags=(),
                attachments=(),
            ),
            bad_reason_contains="received_at",
            label="judge_simple_text",
        )

        # ── judge_email_index_fetched_batch ────────────────────────
        good_entry = dm.EmailIndexFetchedEntry(
            provider_message_id=_GOOD_PROVIDER_ID,
            thread_id="thread_abc12",
            from_address="alice@example.com",
            from_display_clean="Alice",
            date_iso="2026-05-20T14:00:00+00:00",
            subject_raw="hello",
            gmail_snippet="hi there",
            body_text_or_null="",
            has_attachment=False,
            labels=("INBOX",),
        )
        self._case(
            judges.judge_email_index_fetched_batch,
            good=dm.EmailIndexFetchedBatch(
                phase="1",
                batch_id="batch_001",
                watermark_before="9000",
                watermark_after="9100",
                entries=(good_entry,),
                fetched_count=1,
                skipped_count=0,
                error_kind="",
            ),
            bad=dm.EmailIndexFetchedBatch(
                phase="not-a-phase",  # not in closed enum
                batch_id="batch_001",
                watermark_before="9000",
                watermark_after="9100",
                entries=(good_entry,),
                fetched_count=1,
                skipped_count=0,
                error_kind="",
            ),
            bad_reason_contains="phase",
            label="judge_email_index_fetched_batch",
        )

        # ── judge_email_index_batch (the audit receipt) ────────────
        self._case(
            judges.judge_email_index_batch,
            good=dm.EmailIndexBatchReceipt(
                phase="1",
                batch_id="batch_001",
                watermark_before="9000",
                watermark_after="9100",
                embedded_count=1,
                error_count=0,
                sample_error_message="",
            ),
            bad=dm.EmailIndexBatchReceipt(
                phase="1",
                batch_id="!!",  # malformed batch_id
                watermark_before="9000",
                watermark_after="9100",
                embedded_count=1,
                error_count=0,
                sample_error_message="",
            ),
            bad_reason_contains="batch_id",
            label="judge_email_index_batch",
        )

        return self.result(
            "8 JUDGE handlers each approve a golden extract and reject "
            "a one-field-wrong extract citing the violated field."
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _case(
        self,
        judge_fn,
        *,
        good,
        bad,
        bad_reason_contains: str,
        label: str,
    ) -> None:
        good_verdict = judge_fn(good)
        self.assert_that(
            getattr(good_verdict, "approved", None) is True,
            f"{label}: must APPROVE the golden valid input; got: "
            f"{good_verdict!r}",
        )
        bad_verdict = judge_fn(bad)
        self.assert_that(
            getattr(bad_verdict, "approved", None) is False,
            f"{label}: must REJECT the malformed input; got: "
            f"{bad_verdict!r}",
        )
        reason = getattr(bad_verdict, "reason", "") or ""
        self.assert_that(
            bad_reason_contains in reason,
            f"{label}: rejection reason must mention "
            f"{bad_reason_contains!r}; got: {reason!r}",
        )
