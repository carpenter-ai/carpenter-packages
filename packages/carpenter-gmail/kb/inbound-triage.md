# Inbound email triage (carpenter-gmail Phase 3a)

The `gmail_poll` trigger polls Gmail's `users.history.list` API every
15 minutes (configurable per install) and emits one `email.received`
event per newly-arrived message id.  The package's
`trigger_subscriptions` then routes each event into the
`email_triage` arc template.

## What the chat agent sees

After a triage arc completes, the chat agent receives a
`EmailTriageExtract` with the following fields:

- `provider_message_id` — opaque Gmail message id
- `received_history_id` — the Gmail `historyId` watermark
- `category` — one of `personal`, `transactional`, `newsletter`,
  `promotional`, `automated`, `unknown`
- `from_address` — sender's email (allowlist-validated by the
  platform's JUDGE dispatch wrapper)
- `subject_clean` — sanitised subject (control characters stripped,
  URLs forbidden, max 200 characters)
- `importance_flags` — bounded tuple from the closed enum
  `{high_priority, newsletter, promotional, automated, personal,
  suspicious_keyword}`

The chat agent does NOT receive the raw email body, header text, or
unstripped subject.  If you need the body, use the existing read
templates (`pkg_gmail_read_*`) which fan a separate arc with full
trust pipeline.

## Trust contract

The triage REVIEWER runs with a static, package-shipped prompt that
explicitly forbids copying raw header / body / snippet text into the
extract.  Subject is required to be in the sanitised form.  The
deterministic `judge_email_triage` Python handler re-validates every
field before the Resource graduates to trusted state.

Concretely: if a hostile email tries to smuggle a prompt-injection
payload through the subject ("Ignore prior instructions and ..."),
either

1. the REVIEWER's static prompt rejects it because the category
   choice is bound by closed-enum rules, not message content; or
2. the JUDGE rejects the extract because the subject contains
   control characters / URLs / exceeds 200 chars.

## Auth revocation

If the Gmail API returns 401 (token revoked, scope withdrawn, etc.),
the trigger emits ONE `email.auth_revoked` event and stops polling
in-process until the daemon restarts.  Run `pkg_gmail_authorize` to
restore credentials, then restart the daemon (`systemctl --user
restart carpenter`) — the trigger picks up the new token at next
heartbeat.

## Rate limiting

On HTTP 429 / 5xx the trigger stores a `gmail_poll_backoff_until`
timestamp in `package_state` (default 1 hour) and skips polls until
elapsed.  Backoff clears automatically on the next successful poll.

## Backfill

Phase 3a v1 deliberately does NOT backfill historical messages.  The
trigger starts polling from "now" — the `users.getProfile` call on
first run captures the current `historyId` and uses that as the
initial watermark.  Backfill of older messages is reserved for a
future phase.
