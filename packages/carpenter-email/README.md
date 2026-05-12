# carpenter-email

D24 capability package: Gmail read + send for the chat agent, with
the full PLANNER -> EXECUTOR -> REVIEWER -> JUDGE pipeline gating
every U->T (untrusted-to-trusted) graduation.

**If you just want to use the package**, read [`SETUP.md`](SETUP.md)
— it walks an end user from Google Cloud setup through first send.
This README is package-developer / package-author oriented.

## Layout

```
manifest.yaml                       # package descriptor
data_models.py                      # EmailReviewBriefing + 3 extracts
judges.py                           # 3 deterministic JUDGE handlers
scripts.py                          # pre-verified EXECUTOR scripts
tools.py                            # @chat_tool functions
templates/
  email-read-simple-text/
    template.yaml
    reviewer.txt
  email-read-meeting-invite/
    template.yaml
    reviewer.txt
  email-read-order-confirmation/
    template.yaml
    reviewer.txt
kb/
  overview.md
  policy-setup.md
  trust-warning.md
  style.md
```

## Install

This package is loaded by the platform's `carpenter.packages`
machinery via the standard install flow:

1. The platform's package installer reads `manifest.yaml`.
2. `data_models` are registered with the JUDGE-dispatch deserialiser.
3. `arc_templates` are loaded into the platform's template store.
4. `judge_handlers` are wired into the handler registry.
5. `chat_tools` (`tools.py`) are imported and the `@chat_tool`-
   decorated functions are registered.
6. `kb_articles` are copied into the platform KB under `email/*`.
7. `allowlist_proposals` (gmail.googleapis.com,
   oauth2.googleapis.com) are presented to the operator for
   confirmation.
8. `credential_requirements` triggers the OAuth-credential one-time
   link UI, where the operator pastes a Google Cloud OAuth
   client_id / client_secret pair.

## First-run

After install, the user must run `pkg_email_authorize` (a chat tool)
to complete the Google OAuth round-trip.  The platform stores
access/refresh tokens in `.env` under `GMAIL_OAUTH_*`.

## Phase 1 vs later phases

This Phase 1 package ships:

* Three read templates with REVIEWER + JUDGE.
* `pkg_email_send_email` with chat-confirm + allowlist + expected-
  account check.
* Allowlist mutation tools (`pkg_email_trust_sender`,
  `pkg_email_untrust_sender`).
* OAuth bootstrap (`pkg_email_authorize`).

Future phases (not in this package):

* Phase 1.5: archive / mark_read / draft.
* Phase 3: trigger subscriptions for inbound polling, IMAP backend
  alternative.

See the build plan in carpenter-core/docs (PR #310).
