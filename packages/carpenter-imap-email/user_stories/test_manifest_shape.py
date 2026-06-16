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
* the five TRUSTED platform_capabilities (imap.fetch / imap.search /
  imap.store / imap.append / smtp.send) with their egress grants,
* the single kind:env credential requirement with the eight
  EMAIL_* keys,
* the confirmed mailbox.org allowlist proposals,
* the KB articles covering the trust-contract surfaces,
* (v0.2.0) the inbound-poll trigger (imap_poll) + the email.received
  triage subscription + the email_triage arc template + the
  judge_email_triage handler that the inbound path needs.

It deliberately asserts that the package STILL does NOT declare the
deferred semantic-index triggers / templates (those remain composed in
but not wired up).
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
        # v0.2.0 inbound triage template.
        "email_triage": (
            "EmailTriageExtract", "judges:judge_email_triage",
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
        # v0.2.0 inbound triage JUDGE.
        "judge_email_triage",
    })

    # verb -> (handler, protocol, host_from, port, credential_ref)
    _EXPECTED_CAPABILITIES = {
        "imap.fetch": ("handle_imap_fetch", "imaps", "IMAP_HOST", 993, "EMAIL"),
        "imap.search": ("handle_imap_search", "imaps", "IMAP_HOST", 993, "EMAIL"),
        "imap.store": ("handle_imap_store", "imaps", "IMAP_HOST", 993, "EMAIL"),
        "imap.append": ("handle_imap_append", "imaps", "IMAP_HOST", 993, "EMAIL"),
        "smtp.send": ("handle_smtp_send", "smtps", "SMTP_HOST", 465, "EMAIL"),
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
        # v0.2.0: inbound-triage trust contract, seeded now that the
        # email.received subscription is wired.
        "email/inbound-triage",
    })

    # v0.2.0 trigger + subscription expectations.
    # trigger_name -> (trigger_type, cadence_seconds)
    _EXPECTED_TRIGGERS = {
        "imap-inbound-poll": ("imap_poll", 900),
    }

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
        # The semantic-index templates remain DEFERRED (not declared).
        # email_triage IS now declared (v0.2.0 inbound path), so it is
        # asserted present via _EXPECTED_TEMPLATES above.
        for deferred in (
            "email_index_phase1", "email_index_phase2",
            "email_index_incremental",
        ):
            self.assert_that(
                deferred not in templates_by_name,
                f"deferred index template {deferred!r} must NOT be declared",
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
        self.assert_equal(cred.env_key_prefix, "EMAIL", "credential.env_key_prefix")
        declared_keys = set(cred.required_keys)
        self.assert_equal(
            declared_keys, self._EXPECTED_CRED_KEYS, "credential required_keys",
        )

        # ── Allowlist proposals (provisional mailbox.org) ──────────
        declared_allow = {(a.policy_type, a.value) for a in manifest.allowlist_proposals}
        self.assert_equal(
            declared_allow, self._EXPECTED_ALLOWLIST, "allowlist_proposals",
        )

        # ── v0.2.0 inbound-poll trigger ────────────────────────────
        triggers_by_name = {t.name: t for t in manifest.triggers}
        for tname, (ttype, cadence) in self._EXPECTED_TRIGGERS.items():
            self.assert_that(
                tname in triggers_by_name,
                f"manifest.triggers missing {tname!r}; declared: "
                f"{sorted(triggers_by_name)}",
            )
            tr = triggers_by_name[tname]
            self.assert_equal(tr.type, ttype, f"trigger {tname!r}.type")
            self.assert_equal(
                (tr.config or {}).get("cadence_seconds"), cadence,
                f"trigger {tname!r}.cadence_seconds",
            )
            self.assert_equal(
                (tr.config or {}).get("event_type"), "email.received",
                f"trigger {tname!r}.event_type",
            )
            # Folder policy: INBOX watched by default, Junk NOT watched.
            folders = (tr.config or {}).get("folders") or []
            self.assert_that(
                "INBOX" in folders,
                f"trigger {tname!r}: INBOX must be in watched folders; "
                f"got {folders!r}",
            )
            self.assert_that(
                "Junk" not in folders,
                f"trigger {tname!r}: Junk (spam) must NOT be watched by "
                f"default; got {folders!r}",
            )

        # The semantic-index triggers remain DEFERRED.
        for deferred_trigger in (
            "imap-index-phase1", "imap-index-phase2", "imap-index-incremental",
        ):
            self.assert_that(
                deferred_trigger not in triggers_by_name,
                f"deferred index trigger {deferred_trigger!r} must NOT be "
                f"declared",
            )

        # ── email.received triage subscription ─────────────────────
        triage_subs = [
            s for s in manifest.trigger_subscriptions
            if s.event == "email.received"
            and s.handler == "handlers.triage_inbound:handle_email_received"
        ]
        self.assert_equal(
            len(triage_subs), 1,
            "manifest must declare exactly one email.received subscription "
            "routed to handlers.triage_inbound:handle_email_received; got: "
            f"{[(s.event, s.handler) for s in manifest.trigger_subscriptions]}",
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
            f"the EMAIL env credential, "
            f"{len(declared_slugs)} KB articles, "
            f"{len(triggers_by_name)} inbound-poll trigger(s), and the "
            f"email.received triage subscription; semantic-index triggers "
            f"remain deferred."
        )
