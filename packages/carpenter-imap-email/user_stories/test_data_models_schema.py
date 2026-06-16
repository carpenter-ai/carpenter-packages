"""
Package-internal: carpenter-imap-email dataclass schema contract.

Verifies that every kind declared by ``manifest.data_models``:

* Is importable from the package's ``data_models.py``.
* Is a frozen dataclass (``@dataclass(frozen=True)``).
* Carries a ``schema_version`` field defaulting to ``"1.0"``.
* Carries the expected required-field set (no silent renames).

This is the package's own contract for its trust-graduating
dataclasses.  A REVIEWER reads briefing + untrusted input and
constructs ONE of these per template; the JUDGE bounds-checks every
field.  Renaming or dropping a field here is a breaking change for
the entire JUDGE / chat-notify path; this story is the noisy
fence-post for that.
"""

from __future__ import annotations

import dataclasses

from ._framework import PackageStory, StoryResult, load_package_module


class DataModelsSchema(PackageStory):
    name = "carpenter-imap-email::data_models_schema"
    description = (
        "Every dataclass in data_models.py is frozen, has "
        "schema_version='1.0', and exposes the expected field set."
    )

    # Source of truth for the expected shape.  Tuples of required
    # field names (not exhaustive — JUDGE handlers check the rest).
    _EXPECTED_FIELDS: dict[str, frozenset[str]] = {
        "AttachmentMetadata": frozenset({
            "filename_clean", "claimed_mime_type", "size_bytes",
            "attachment_id", "is_inline", "schema_version",
        }),
        "EmailReviewBriefing": frozenset({
            "expected_account_email", "senders_to_trust",
            "suspicious_keywords", "extract_schema_version",
            "staged_to_addresses",
        }),
        "EmailSimpleTextExtract": frozenset({
            "provider_message_id", "expected_account_email",
            "from_address", "to_addresses", "cc_addresses", "subject",
            "received_at", "body_summary", "extracted_urls", "flags",
            "attachments", "schema_version",
        }),
        "EmailMeetingInviteExtract": frozenset({
            "provider_message_id", "expected_account_email",
            "from_address", "to_addresses", "cc_addresses", "subject",
            "received_at", "start_at", "end_at", "location",
            "organizer", "body_summary", "flags", "attachments",
            "schema_version",
        }),
        "EmailOrderConfirmationExtract": frozenset({
            "provider_message_id", "expected_account_email",
            "from_address", "to_addresses", "cc_addresses", "subject",
            "received_at", "vendor", "total", "order_id", "items",
            "body_summary", "flags", "attachments", "schema_version",
        }),
        "EmailSendResult": frozenset({
            "status", "expected_account_email", "provider_message_id",
            "to_addresses", "schema_version",
        }),
        "EmailArchiveResult": frozenset({
            "status", "expected_account_email", "provider_message_id",
            "was_already_archived", "schema_version",
        }),
        "EmailMarkReadResult": frozenset({
            "status", "expected_account_email", "provider_message_id",
            "was_already_read", "schema_version",
        }),
        "EmailDraftResult": frozenset({
            "status", "expected_account_email", "provider_message_id",
            "draft_id", "to_addresses", "schema_version",
        }),
        "EmailTriageExtract": frozenset({
            "provider_message_id", "received_history_id", "category",
            "from_address", "subject_clean", "importance_flags",
            "attachments", "schema_version",
        }),
        "EmailIndexFetchedEntry": frozenset({
            "provider_message_id", "thread_id", "from_address",
            "from_display_clean", "date_iso", "subject_raw",
            "gmail_snippet", "body_text_or_null", "has_attachment",
            "labels", "schema_version",
        }),
        "EmailIndexFetchedBatch": frozenset({
            "phase", "batch_id", "watermark_before", "watermark_after",
            "entries", "fetched_count", "skipped_count", "error_kind",
            "schema_version",
        }),
        "EmailIndexBatchReceipt": frozenset({
            "phase", "batch_id", "watermark_before", "watermark_after",
            "embedded_count", "error_count", "sample_error_message",
            "schema_version",
        }),
    }

    def run(self, client=None, db=None) -> StoryResult:
        dm = load_package_module("data_models")

        for kind_name, expected_fields in self._EXPECTED_FIELDS.items():
            cls = getattr(dm, kind_name, None)
            self.assert_that(
                cls is not None,
                f"data_models.py missing class {kind_name!r}",
            )
            self.assert_that(
                dataclasses.is_dataclass(cls),
                f"{kind_name}: must be a @dataclass",
            )
            # Frozen check: dataclasses store this on the
            # __dataclass_params__ attribute.
            params = getattr(cls, "__dataclass_params__", None)
            self.assert_that(
                params is not None and params.frozen,
                f"{kind_name}: must be @dataclass(frozen=True); "
                f"params={params!r}",
            )
            # Field set
            actual_fields = {f.name for f in dataclasses.fields(cls)}
            missing = expected_fields - actual_fields
            extra = actual_fields - expected_fields
            self.assert_that(
                not missing,
                f"{kind_name}: missing fields {sorted(missing)}; "
                f"got {sorted(actual_fields)}",
            )
            self.assert_that(
                not extra,
                f"{kind_name}: unexpected fields {sorted(extra)} "
                f"(if intentional, update the expected set in this "
                f"story); got {sorted(actual_fields)}",
            )
            # Schema version default == "1.0".  EmailReviewBriefing
            # uses the historical name ``extract_schema_version`` for
            # the same purpose; every other dataclass uses the
            # canonical ``schema_version``.
            sv_name = (
                "extract_schema_version"
                if kind_name == "EmailReviewBriefing"
                else "schema_version"
            )
            sv_field = next(
                (
                    f for f in dataclasses.fields(cls)
                    if f.name == sv_name
                ),
                None,
            )
            self.assert_that(
                sv_field is not None,
                f"{kind_name}: must expose a {sv_name!r} field",
            )
            self.assert_that(
                sv_field.default == "1.0",
                f"{kind_name}: {sv_name} default must be '1.0'; "
                f"got {sv_field.default!r}",
            )

        return self.result(
            f"All {len(self._EXPECTED_FIELDS)} dataclasses are frozen, "
            f"carry schema_version='1.0', and expose the expected "
            f"field sets."
        )
