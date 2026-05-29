"""
Package-internal: carpenter-gmail manifest shape.

This story is the package's own contract test for its ``manifest.yaml``.
The platform-level acceptance stories (s055-s058) used to assert this
shape too, which created a cross-repo coupling: bumping the package
version forced a same-PR change in carpenter-linux even though no
platform behaviour had changed.  The platform stories now check only
integration behaviour and trust the package's own stories to own the
manifest contract.

What this story verifies:

* ``manifest.yaml`` loads cleanly.
* The package version is present and parseable.
* The expected chat-tool source file is declared.
* The expected dataclass kinds are declared (the trust-graduating
  set, plus the sub-component kinds).
* The expected arc templates are declared, each with the matching
  ``extract_kind`` and ``judge_handler`` wiring.
* All declared JUDGE handlers are present.
* The required OAuth scopes are declared (one credential requirement
  with five gmail.* scopes).
* The expected trigger types are declared with sensible cadences.
* The ``email.received`` trigger subscription routes to the expected
  handler.
* The KB articles cover the trust-contract surfaces.
"""

from __future__ import annotations

from ._framework import (
    PackageStory,
    StoryResult,
    ensure_carpenter_on_path,
    package_root,
)


class ManifestShape(PackageStory):
    name = "carpenter-gmail::manifest_shape"
    description = (
        "carpenter-gmail manifest declares the expected data models, "
        "arc templates, JUDGE handlers, OAuth scopes, triggers, and "
        "trigger subscriptions."
    )

    # Sources of truth for the contract — keep these in sync with
    # manifest.yaml.  The platform stories no longer pin a specific
    # version string; this story does.

    _EXPECTED_DATA_MODELS = frozenset({
        "EmailReviewBriefing",
        "EmailSimpleTextExtract",
        "EmailMeetingInviteExtract",
        "EmailOrderConfirmationExtract",
        "EmailSendResult",
        "EmailArchiveResult",
        "EmailMarkReadResult",
        "EmailDraftResult",
        "EmailTriageExtract",
        "AttachmentMetadata",
        "EmailIndexFetchedEntry",
        "EmailIndexFetchedBatch",
        "EmailIndexBatchReceipt",
    })

    _EXPECTED_TEMPLATES = {
        # template_name -> (extract_kind, judge_handler)
        "email_read_simple_text": (
            "EmailSimpleTextExtract", "judges:judge_simple_text",
        ),
        "email_read_meeting_invite": (
            "EmailMeetingInviteExtract", "judges:judge_meeting_invite",
        ),
        "email_read_order_confirmation": (
            "EmailOrderConfirmationExtract",
            "judges:judge_order_confirmation",
        ),
        "email_write_send": (
            "EmailSendResult", "judges:judge_email_send",
        ),
        "email_write_archive": (
            "EmailArchiveResult", "judges:judge_email_archive",
        ),
        "email_write_mark_read": (
            "EmailMarkReadResult", "judges:judge_email_mark_read",
        ),
        "email_write_draft": (
            "EmailDraftResult", "judges:judge_email_draft",
        ),
        "email_triage": (
            "EmailTriageExtract", "judges:judge_email_triage",
        ),
        "email_index_phase1": (
            "EmailIndexFetchedBatch",
            "judges:judge_email_index_fetched_batch",
        ),
        "email_index_phase2": (
            "EmailIndexFetchedBatch",
            "judges:judge_email_index_fetched_batch",
        ),
        "email_index_incremental": (
            "EmailIndexFetchedBatch",
            "judges:judge_email_index_fetched_batch",
        ),
    }

    _EXPECTED_JUDGES = frozenset({
        "judge_simple_text",
        "judge_meeting_invite",
        "judge_order_confirmation",
        "judge_email_send",
        "judge_email_archive",
        "judge_email_mark_read",
        "judge_email_draft",
        "judge_email_triage",
        "judge_email_index_fetched_batch",
        "judge_email_index_batch",
    })

    _EXPECTED_SCOPES = frozenset({
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/userinfo.email",
    })

    _EXPECTED_TRIGGERS = {
        # trigger_name -> (trigger_type, cadence_seconds)
        "gmail-inbound-poll": ("gmail_poll", 900),
        "gmail-index-phase1": ("email_index_phase1", 60),
        "gmail-index-phase2": ("email_index_phase2", 60),
        "gmail-index-incremental": ("email_index_incremental", 60),
    }

    _EXPECTED_KB_SLUGS = frozenset({
        "email/overview",
        "email/policy-setup",
        "email/trust-warning",
        "email/style",
        "email/inbound-triage",
        "email/attachments",
        "email/index",
        "email/search",
    })

    def run(self, client=None, db=None) -> StoryResult:
        ensure_carpenter_on_path()
        from carpenter.packages.manifest import load_manifest

        manifest = load_manifest(package_root() / "manifest.yaml")

        # ── Version ────────────────────────────────────────────────
        self.assert_that(
            isinstance(manifest.version, str) and manifest.version,
            f"manifest.version must be a non-empty string; got "
            f"{manifest.version!r}",
        )
        # Three-segment semantic version, e.g. "0.7.0".
        parts = manifest.version.split(".")
        self.assert_that(
            len(parts) == 3 and all(p.isdigit() for p in parts),
            f"manifest.version must look like MAJOR.MINOR.PATCH; got "
            f"{manifest.version!r}",
        )

        # ── chat_tools entrypoint ──────────────────────────────────
        self.assert_that(
            tuple(manifest.chat_tools) == ("tools.py",),
            f"manifest.chat_tools must be ('tools.py',); got "
            f"{manifest.chat_tools!r}",
        )

        # ── Data models ────────────────────────────────────────────
        declared_models = set(manifest.data_models)
        missing_models = self._EXPECTED_DATA_MODELS - declared_models
        self.assert_that(
            not missing_models,
            f"manifest.data_models missing expected kinds: "
            f"{sorted(missing_models)}; declared: "
            f"{sorted(declared_models)}",
        )

        # ── Arc templates: name, extract_kind, judge_handler ───────
        templates_by_name = {t.name: t for t in manifest.arc_templates}
        for tname, (extract_kind, judge_handler) in (
            self._EXPECTED_TEMPLATES.items()
        ):
            self.assert_that(
                tname in templates_by_name,
                f"manifest.arc_templates missing {tname!r}; declared: "
                f"{sorted(templates_by_name)}",
            )
            tmpl = templates_by_name[tname]
            self.assert_that(
                tmpl.extract_kind == extract_kind,
                f"template {tname!r}: extract_kind expected "
                f"{extract_kind!r}, got {tmpl.extract_kind!r}",
            )
            self.assert_that(
                tmpl.judge_handler == judge_handler,
                f"template {tname!r}: judge_handler expected "
                f"{judge_handler!r}, got {tmpl.judge_handler!r}",
            )

        # ── JUDGE handlers declared ────────────────────────────────
        declared_judges = {h.name for h in manifest.judge_handlers}
        missing_judges = self._EXPECTED_JUDGES - declared_judges
        self.assert_that(
            not missing_judges,
            f"manifest.judge_handlers missing: "
            f"{sorted(missing_judges)}; declared: "
            f"{sorted(declared_judges)}",
        )

        # ── OAuth scopes ───────────────────────────────────────────
        self.assert_that(
            len(manifest.credential_requirements) == 1,
            f"manifest must declare exactly one credential_requirement; "
            f"got {len(manifest.credential_requirements)}",
        )
        cred = manifest.credential_requirements[0]
        declared_scopes = set(cred.scopes)
        missing_scopes = self._EXPECTED_SCOPES - declared_scopes
        self.assert_that(
            not missing_scopes,
            f"credential_requirement missing scopes: "
            f"{sorted(missing_scopes)}; declared: "
            f"{sorted(declared_scopes)}",
        )

        # ── Triggers ───────────────────────────────────────────────
        triggers_by_name = {t.name: t for t in manifest.triggers}
        for tname, (ttype, cadence) in self._EXPECTED_TRIGGERS.items():
            self.assert_that(
                tname in triggers_by_name,
                f"manifest.triggers missing {tname!r}; declared: "
                f"{sorted(triggers_by_name)}",
            )
            tr = triggers_by_name[tname]
            self.assert_that(
                tr.type == ttype,
                f"trigger {tname!r}: type expected {ttype!r}, "
                f"got {tr.type!r}",
            )
            actual_cadence = (tr.config or {}).get("cadence_seconds")
            self.assert_that(
                actual_cadence == cadence,
                f"trigger {tname!r}: cadence_seconds expected "
                f"{cadence!r}, got {actual_cadence!r}",
            )

        # ── Trigger subscriptions ──────────────────────────────────
        # At minimum, ``email.received`` must route to the
        # ``handlers.triage_inbound:handle_email_received`` shim.
        triage_subs = [
            s for s in manifest.trigger_subscriptions
            if s.event == "email.received"
            and s.handler
            == "handlers.triage_inbound:handle_email_received"
        ]
        self.assert_that(
            len(triage_subs) == 1,
            f"manifest must declare exactly one email.received "
            f"subscription routed to handlers.triage_inbound:"
            f"handle_email_received; got: "
            f"{[(s.event, s.handler) for s in manifest.trigger_subscriptions]}",
        )

        # ── KB articles ────────────────────────────────────────────
        declared_slugs = {a.slug for a in manifest.kb_articles}
        missing_slugs = self._EXPECTED_KB_SLUGS - declared_slugs
        self.assert_that(
            not missing_slugs,
            f"manifest.kb_articles missing: {sorted(missing_slugs)}; "
            f"declared: {sorted(declared_slugs)}",
        )

        return self.result(
            f"carpenter-gmail manifest v{manifest.version} declares "
            f"{len(declared_models)} data models, "
            f"{len(templates_by_name)} arc templates, "
            f"{len(declared_judges)} JUDGE handlers, "
            f"{len(triggers_by_name)} triggers, and "
            f"{len(declared_slugs)} KB articles."
        )
