# carpenter-imap-email — setup & first-use guide

This is the end-user guide for the `carpenter-imap-email` package.  It
connects Carpenter to a plain IMAP/SMTP mailbox so the chat agent can
read, search, send, reply, archive, and mark-read mail — without ever
letting a raw email body into the agent's context.

If you're a package developer, read [`README.md`](README.md) instead.

## What you get (v0.1.0)

- **Search / list / read** through a structured-extract pipeline that
  never exposes raw bodies to the chat agent.
- **Send / reply** with a per-recipient allowlist and a human-confirm
  prompt at the chat boundary.
- **Archive / mark-read** (idempotent, confirm-gated).
- **Trust management** for the sender/recipient allowlist.

Deliberately NOT in this version: inbound mail polling, semantic/vector
search, provider-native drafts.  See README "DEFERRED".

## 1. Prerequisites: a mailbox + app password

You need an IMAP/SMTP mailbox account and an **app password** (most
providers require an app-specific password rather than your login
password for IMAP/SMTP access).

> **Provider status — PROVISIONAL.** This package proposes
> `imap.mailbox.org` / `smtp.mailbox.org` as allowlist entries, but the
> production provider has **not been finalised**.  The handlers work
> against any provider that speaks **IMAPS on port 993** and **SMTPS on
> port 465**.  Use whatever mailbox you actually have; supply its real
> host + credentials below.

Provision the account at your provider and create an app password.  Note
down:

- IMAP host (e.g. `imap.mailbox.org`) — must accept IMAPS on 993.
- SMTP host (e.g. `smtp.mailbox.org`) — must accept SMTPS on 465.
- The login username (usually your full email address).
- The app password.

## 2. Install the package

In the Carpenter chat:

> **You:** "Install the carpenter-imap-email package."

The platform's installer reads `manifest.yaml` and:

1. Registers the 13 data-model dataclasses with the JUDGE-dispatch
   deserialiser.
2. Loads the 7 arc templates (3 read + 4 write), stamped
   `owner_package=carpenter-imap-email`.
3. Wires the deterministic JUDGE handlers.
4. Registers the `pkg_imap_*` chat tools.
5. Seeds the KB articles under `email/*`.
6. Presents the two **provisional** allowlist additions
   (`imap.mailbox.org`, `smtp.mailbox.org`) for your one-time
   confirmation — replace or confirm to match your real provider.
7. **Confirms the four platform capabilities** (`imap.fetch`,
   `imap.search`, `imap.store`, `smtp.send`).  Because these are
   TRUSTED platform-side handlers (they run with egress + credentials),
   the operator approves each grant interactively — this is
   platform-level trust, a higher bar than an ordinary chat tool.
8. Surfaces the credential UI for the eight `IMAP_EMAIL_*` values.

## 3. Supply the credentials

When the credential UI opens, paste these eight values:

| Key                        | Value                              |
|----------------------------|------------------------------------|
| `IMAP_EMAIL_IMAP_HOST`     | your IMAP host (e.g. imap.mailbox.org) |
| `IMAP_EMAIL_IMAP_PORT`     | `993`                              |
| `IMAP_EMAIL_IMAP_USERNAME` | your mailbox login                 |
| `IMAP_EMAIL_IMAP_PASSWORD` | your IMAP app password             |
| `IMAP_EMAIL_SMTP_HOST`     | your SMTP host (e.g. smtp.mailbox.org) |
| `IMAP_EMAIL_SMTP_PORT`     | `465`                              |
| `IMAP_EMAIL_SMTP_USERNAME` | your mailbox login                 |
| `IMAP_EMAIL_SMTP_PASSWORD` | your SMTP app password             |

These resolve **platform-side** when a trusted handler runs (via
`CapabilityContext.secret`).  They are never injected into the untrusted
executor, never embedded in a script, and never logged.

`IMAP_EMAIL_IMAP_HOST` / `IMAP_EMAIL_SMTP_HOST` also bind the egress
host for the capability grants, so the operator confirms a concrete
`host:port` and the handler can never point egress elsewhere.

There is no separate "authorize" step — unlike Gmail's OAuth flow, the
IMAP/SMTP backend is functional as soon as the eight credentials are
set.

## 4. First use

```
You:  "Search my mailbox for invoices."
      → pkg_imap_search_emails(query="invoice")  → list of UIDs

You:  "Read the first one."
      → pkg_imap_read_email(provider_message_id="<uid>", kind="order_confirmation")
      → JUDGE-approved EmailOrderConfirmationExtract (no raw body)

You:  "Reply saying thanks."
      → pkg_imap_reply_email(...)  → chat-boundary confirm → smtp.send
```

To send to a new recipient you must first trust them:
`pkg_imap_trust_sender("alice@example.com")` (human-confirmed).

## 5. Trust model — what the package will NOT do

- **Never trusts a sender you didn't trust.** The `SecurityPolicies.email`
  allowlist ships empty; until you add a sender, their messages are
  rejected at JUDGE time and you cannot send to them.
- **Never puts credentials or a host in the executor.** Egress is the
  trusted capability handler's job; the executor only dispatches verbs.
- **Never re-feeds extracted strings back into LLM context** as
  instructions — subject lines / sender names / body summaries are
  bounded, control-char-filtered data you read.
- **Never sends from an arbitrary From** — the SMTP envelope sender is
  always the authenticated account.

## 6. Troubleshooting

- **"expected_account is not configured"** — you haven't set
  `IMAP_EMAIL_IMAP_USERNAME` (or `operator_email`).  The read/send tools
  fail closed without an expected account.
- **`imap.fetch failed` / `smtp.send failed`** — check the host/port and
  app password; confirm your provider allows IMAPS/SMTPS app-password
  access.  Errors never include the credential.
- **Recipient rejected at the chat boundary** — run
  `pkg_imap_trust_sender(...)` for that address.
