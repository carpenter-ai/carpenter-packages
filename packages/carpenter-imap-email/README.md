# carpenter-imap-email

Capability package: IMAP/SMTP email read + send for the Carpenter chat
agent, with the full PLANNER → EXECUTOR → REVIEWER → JUDGE pipeline
gating every untrusted-to-trusted (U→T) graduation.

This is the **first real consumer of Carpenter's package-capability
framework**.  Where `carpenter-gmail` reaches Google from inside the
untrusted EXECUTOR (OAuth bearer from `os.environ`, hardcoded
`gmail.googleapis.com`), this package declares **trusted platform-side
dispatch verbs** — `imap.fetch`, `imap.search`, `imap.store`,
`smtp.send` — whose handlers run parent-side with the mailbox host +
credentials the operator confirmed at install.  The untrusted EXECUTOR
scripts are **cred-free and host-free**.

**If you just want to use the package**, read [`SETUP.md`](SETUP.md).
This README is package-developer oriented.

## Layout

```
compose.yaml                  # composes from layers/carpenter-email-core
manifest.yaml                 # package descriptor (capabilities, creds, ...)
handlers/
  imap_smtp.py                # TRUSTED capability handlers (the new code)
  triage_inbound.py           # composed (DEFERRED — not wired up)
  __init__.py
scripts.py                    # cred-free / host-free EXECUTOR scripts
tools.py                      # @chat_tool functions (pkg_imap_*)
arc_builders.py               # composed: backend-agnostic arc builders
data_models.py                # composed: 13 trust-graduating dataclasses
judges.py                     # composed: deterministic JUDGE handlers
templates/email-*             # composed: PLANNER/EXECUTOR/REVIEWER/JUDGE
kb/
  overview.md                 # leaf (IMAP-specific)
  policy-setup.md             # leaf (IMAP-specific)
  search.md                   # leaf (IMAP-specific)
  trust-warning.md style.md attachments.md   # composed (shared trust KB)
  inbound-triage.md index.md  # composed (DEFERRED features' KB)
user_stories/                 # 5 package-internal acceptance stories
```

## Composition

The shared email trust pipeline lives in
`layers/carpenter-email-core/`.  `compose.yaml` declares
`compose_from: [carpenter-email-core]`; the leaf physically ships
byte-identical copies of every layer file.

```
python -m tools.compose verify packages/carpenter-imap-email
```

proves the copies are faithful (drift guard).  Backend-specific files
(`manifest.yaml`, `handlers/imap_smtp.py`, `scripts.py`, `tools.py`, the
three IMAP KB articles, README/SETUP) are leaf-only.

## Capability framework — how host + credentials stay out of the executor

The manifest's `platform_capabilities` section declares four trusted
egress verbs.  Each handler in `handlers/imap_smtp.py` is invoked as
`handler(params, ctx)` where:

- `ctx.host` / `ctx.port` / `ctx.protocol` come from the
  operator-confirmed grant (bound from `IMAP_EMAIL_IMAP_HOST` /
  `IMAP_EMAIL_SMTP_HOST` at install).
- `ctx.secret("IMAP_PASSWORD")` etc. resolve `IMAP_EMAIL_<SUFFIX>`
  **platform-side** — never from the untrusted executor environment.
- `params` carries only the operation payload the executor controls
  (uid / mailbox / query / flags / outgoing message), every field
  validated and bounded.  A `host` / `password` in `params` is ignored.

Per-package gating: the package's `arc_templates` are auto-stamped
`owner_package=carpenter-imap-email`, so their EXECUTOR step arcs carry
the `pkg.carpenter-imap-email` grant and pass the per-package dispatch
gate.  An arc that is not from this package's template is **denied**
these verbs.

## MVP scope (v0.1.0)

Read: `pkg_imap_search_emails`, `pkg_imap_list_inbox`,
`pkg_imap_read_email` (three review templates).
Write: `pkg_imap_send_email`, `pkg_imap_reply_email`,
`pkg_imap_archive_email`, `pkg_imap_mark_read_email` (each
confirm-gated, graduated through REVIEWER + JUDGE).
Trust: `pkg_imap_trust_sender` / `pkg_imap_untrust_sender`.

## DEFERRED (NOT built in v0.1.0)

- **Inbound UID-poll trigger + `email.received` triage subscription.**
  The shared `email-triage` template, `handlers/triage_inbound.py`, and
  `kb/inbound-triage.md` are composed in but the manifest does not
  declare the trigger or subscription.  `tools.py` keeps a dormant
  `_create_triage_arc_tree` wrapper so the path lights up cleanly when
  this ships in v0.2.0.
- **Semantic resource index (vector search).** The `email-index-*`
  templates and `kb/index.md` are composed in but not declared; no
  vector store, no index triggers.
- **Provider-native drafts.** IMAP/SMTP has no Gmail-style draft API;
  the MVP `IMAP_DRAFT_SCRIPT` is a best-effort `APPEND`-to-Drafts
  placeholder and the draft tool is intentionally not exposed as a chat
  tool yet (the `email_write_draft` template + JUDGE are wired for the
  future).

## Provider / account status

The proposed allowlist hosts (`imap.mailbox.org`, `smtp.mailbox.org`)
are **PROVISIONAL / unconfirmed** — the production mailbox provider and
account have not been finalised.  The handlers are provider-agnostic
(any IMAPS/SMTPS endpoint works); the operator supplies the real host +
credentials at install.  See `SETUP.md`.

## Testing

- `python -m tools.compose verify packages/carpenter-imap-email`
- Package stories:
  `CARPENTER_PACKAGES_DIR=<repo>/packages python3
  ~/carpenter-dev-tools/acceptance/package_story_runner.py run
  "carpenter-imap-email::"`
- Handler + capability-stack unit tests:
  `tools/tests/test_imap_email_handlers.py` (run via `~/bin/run-tests`).
