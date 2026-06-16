"""
Package-internal: carpenter-imap-email manifest shape.

This story is the package's own contract test for its ``manifest.yaml``.
It mirrors carpenter-gmail's manifest_shape story but asserts the
IMAP/SMTP-specific shape:

* the same 13 trust-graduating data-model kinds (composed from the
  shared carpenter-email-core layer),
* the three read + four write arc templates with their
  extract_kind / judge_handler wiring,
* the read + write JUDGE handlers,
* the four TRUSTED platform_capabilities (imap.fetch / imap.search /
  imap.store / smtp.send) with their egress grants,
* the single kind:env credential requirement with the eight
  IMAP_EMAIL_* keys,
* the provisional mailbox.org allowlist proposals,
* the KB articles covering the trust-contract surfaces.

It deliberately asserts that the package does NOT declare the deferred
inbound-poll trigger / triage subscription or the semantic-index
triggers (those are v0.2.0 follow-ups, composed in but not wired up).
"""

from __future__ import annotations

from ._framework import (
    PackageStory,
    StoryResult,
    ensure_carpenter_on_path,
    package_root,
)


class ManifestShape(PackageStory):
    name = "carpenter-imap-email::manifest_shape"
    description = (
        "carpenter-imap-email manifest declares the expected data "
        "models, arc templates, JUDGE handlers, platform capabilities, "
        "env credentials, and allowlist proposals — and does NOT declare "
        "the deferred triggers / subscriptions."
    )

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
    }

    _EXPECTED_JUDGES = frozenset({
        "judge_simple_text",
        "judge_meeting_invite",
        "judge_order_confirmation",
        "judge_email_send",
        "judge_email_archive",
        "judge_email_mark_read",
        "judge_email_draft",
    })

    # verb -> (handler, protocol, host_from, port, credential_ref)
    _EXPECTED_CAPABILITIES = {
        "imap.fetch": ("handle_imap_fetch", "imaps", "IMAP_HOST", 993, "IMAP_EMAIL"),
        "imap.search": ("handle_imap_search", "imaps", "IMAP_HOST", 993, "IMAP_EMAIL"),
        "imap.store": ("handle_imap_store", "imaps", "IMAP_HOST", 993, "IMAP_EMAIL"),
        "smtp.send": ("handle_smtp_send", "smtps", "SMTP_HOST", 465, "IMAP_EMAIL"),
    }

    _EXPECTED_CRED_KEYS = frozenset({
        "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
    })

    _EXPECTED_ALLOWLIST = frozenset({
        ("domain", "imap.mailbox.org"),
        ("domain", "smtp.mailbox.org"),
    })

    _EXPECTED_KB_SLUGS = frozenset({
        "email/overview",
        "email/policy-setup",
        "email/search",
        "email/trust-warning",
        "email/style",
        "email/attachments",
    })

    def run(self, client=None, db=None) -> StoryResult:
        ensure_carpenter_on_path()
        from carpenter.packages.manifest import load_manifest

        manifest = load_manifest(package_root() / "manifest.yaml")

        # ── Identity / version ─────────────────────────────────────
        self.assert_equal(manifest.name, "carpenter-imap-email", "manifest.name")
        parts = manifest.version.split(".")
        self.assert_that(
            len(parts) == 3 and all(p.isdigit() for p in parts),
            f"version must look like MAJOR.MINOR.PATCH; got {manifest.version!r}",
        )

        # ── chat_tools entrypoint ──────────────────────────────────
        self.assert_that(
            tuple(manifest.chat_tools) == ("tools.py",),
            f"chat_tools must be ('tools.py',); got {manifest.chat_tools!r}",
        )

        # ── Data models ────────────────────────────────────────────
        declared_models = set(manifest.data_models)
        missing_models = self._EXPECTED_DATA_MODELS - declared_models
        self.assert_that(
            not missing_models,
            f"data_models missing: {sorted(missing_models)}",
        )

        # ── Arc templates ──────────────────────────────────────────
        templates_by_name = {t.name: t for t in manifest.arc_templates}
        for tname, (extract_kind, judge_handler) in (
            self._EXPECTED_TEMPLATES.items()
        ):
            self.assert_that(
                tname in templates_by_name,
                f"arc_templates missing {tname!r}; have {sorted(templates_by_name)}",
            )
            tmpl = templates_by_name[tname]
            self.assert_equal(tmpl.extract_kind, extract_kind, f"{tname}.extract_kind")
            self.assert_equal(
                tmpl.judge_handler, judge_handler, f"{tname}.judge_handler",
            )
        # Deferred templates must NOT be declared.
        for deferred in (
            "email_triage", "email_index_phase1", "email_index_phase2",
            "email_index_incremental",
        ):
            self.assert_that(
                deferred not in templates_by_name,
                f"deferred template {deferred!r} must NOT be declared in the MVP",
            )

        # ── JUDGE handlers ─────────────────────────────────────────
        declared_judges = {h.name for h in manifest.judge_handlers}
        missing_judges = self._EXPECTED_JUDGES - declared_judges
        self.assert_that(
            not missing_judges,
            f"judge_handlers missing: {sorted(missing_judges)}",
        )

        # ── Platform capabilities (the new framework surface) ──────
        caps_by_verb = {c.verb: c for c in manifest.platform_capabilities}
        self.assert_equal(
            set(caps_by_verb),
            set(self._EXPECTED_CAPABILITIES),
            "platform_capabilities verbs",
        )
        for verb, (handler, proto, host_from, port, cred_ref) in (
            self._EXPECTED_CAPABILITIES.items()
        ):
            cap = caps_by_verb[verb]
            self.assert_equal(cap.kind, "egress", f"{verb}.kind")
            self.assert_equal(cap.module, "handlers.imap_smtp", f"{verb}.module")
            self.assert_equal(cap.handler, handler, f"{verb}.handler")
            self.assert_equal(cap.grant.protocol, proto, f"{verb}.grant.protocol")
            self.assert_equal(cap.grant.host_from, host_from, f"{verb}.grant.host_from")
            self.assert_equal(cap.grant.port, port, f"{verb}.grant.port")
            self.assert_equal(
                cap.grant.credential_ref, cred_ref, f"{verb}.grant.credential_ref",
            )

        # ── Env credential requirement ─────────────────────────────
        self.assert_equal(
            len(manifest.credential_requirements), 1,
            "exactly one credential_requirement",
        )
        cred = manifest.credential_requirements[0]
        self.assert_equal(cred.kind, "env", "credential.kind")
        self.assert_equal(cred.env_key_prefix, "IMAP_EMAIL", "credential.env_key_prefix")
        declared_keys = set(cred.required_keys)
        self.assert_equal(
            declared_keys, self._EXPECTED_CRED_KEYS, "credential required_keys",
        )

        # ── Allowlist proposals (provisional mailbox.org) ──────────
        declared_allow = {(a.policy_type, a.value) for a in manifest.allowlist_proposals}
        self.assert_equal(
            declared_allow, self._EXPECTED_ALLOWLIST, "allowlist_proposals",
        )

        # ── No deferred triggers / subscriptions ───────────────────
        self.assert_equal(
            len(manifest.triggers), 0,
            "MVP must declare zero triggers (inbound poll + index are deferred)",
        )
        self.assert_equal(
            len(manifest.trigger_subscriptions), 0,
            "MVP must declare zero trigger_subscriptions (triage is deferred)",
        )

        # ── KB articles ────────────────────────────────────────────
        declared_slugs = {a.slug for a in manifest.kb_articles}
        missing_slugs = self._EXPECTED_KB_SLUGS - declared_slugs
        self.assert_that(
            not missing_slugs,
            f"kb_articles missing: {sorted(missing_slugs)}",
        )

        return self.result(
            f"carpenter-imap-email manifest v{manifest.version} declares "
            f"{len(declared_models)} data models, "
            f"{len(templates_by_name)} arc templates, "
            f"{len(caps_by_verb)} platform capabilities, "
            f"the IMAP_EMAIL env credential, and "
            f"{len(declared_slugs)} KB articles, with no deferred "
            f"triggers/subscriptions."
        )
