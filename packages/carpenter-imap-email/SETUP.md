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

## 1. Prerequisites: the mailbox.org account + an Email App Password

The confirmed production account is **`carpenter-ai@mailbox.org`** (a
plain `@mailbox.org` address), reached via:

| Setting   | Value                  |
|-----------|------------------------|
| IMAP host | `imap.mailbox.org`     |
| IMAP port | `993` (IMAPS / TLS)    |
| SMTP host | `smtp.mailbox.org`     |
| SMTP port | `465` (SMTPS / TLS)    |
| Username  | `carpenter-ai@mailbox.org` (both IMAP and SMTP) |

The handlers remain provider-agnostic (any IMAPS-on-993 / SMTPS-on-465
mailbox works), but these are the values this deployment uses.

### Create an Email App Password (mailbox.org)

mailbox.org requires an **app-specific password** for IMAP/SMTP clients;
do not use the web-login password.

1. Sign in to the mailbox.org webmail / settings as
   `carpenter-ai@mailbox.org`.
2. Go to **Settings → Security → App passwords** (mailbox.org calls
   these "Email App Passwords") and create a new one scoped to mail
   (IMAP/SMTP).
3. Copy the generated password — you'll paste it into both
   `EMAIL_IMAP_PASSWORD` and `EMAIL_SMTP_PASSWORD` below.

> **2FA note.** If the web login has two-factor authentication enabled,
> that does **not** block app-password IMAP/SMTP auth — the app password
> is the second factor's stand-in for non-interactive clients.  IMAP/SMTP
> will authenticate with the app password alone; no TOTP prompt is
> involved.

Folder layout on this mailbox: `INBOX, Sent, Drafts, Trash, Junk`.
Guard at-rest encryption is **OFF**, so messages are plaintext-readable
and `imap.fetch` needs no decryption step.

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
6. Presents the two allowlist additions (`imap.mailbox.org`,
   `smtp.mailbox.org`) for your one-time confirmation.  These are now
   the confirmed production hosts; confirm them at the prompt.
7. **Confirms the five platform capabilities** (`imap.fetch`,
   `imap.search`, `imap.store`, `imap.append`, `smtp.send`).  Because
   these are TRUSTED platform-side handlers (they run with egress +
   credentials), the operator approves each grant interactively — this
   is platform-level trust, a higher bar than an ordinary chat tool.
   (`imap.append` is what files the Sent copy — see §6.)
8. Reads the eight `EMAIL_*` values from the per-package `.env`.

## 3. Supply the credentials (per-package `.env`)

The eight `EMAIL_*` values live in the package's own `.env` file:

```
~/carpenter/config/packages/carpenter-imap-email/.env   (chmod 600)
```

`ctx.secret("IMAP_PASSWORD")` resolves `EMAIL_IMAP_PASSWORD` from
this file platform-side.  Populate it with these eight keys (use the
mailbox.org values from §1; the Email App Password goes in both
`*_PASSWORD` keys):

| Key                        | Value                                  |
|----------------------------|----------------------------------------|
| `EMAIL_IMAP_HOST`     | `imap.mailbox.org`                     |
| `EMAIL_IMAP_PORT`     | `993`                                  |
| `EMAIL_IMAP_USERNAME` | `carpenter-ai@mailbox.org`             |
| `EMAIL_IMAP_PASSWORD` | your mailbox.org Email App Password    |
| `EMAIL_SMTP_HOST`     | `smtp.mailbox.org`                     |
| `EMAIL_SMTP_PORT`     | `465`                                  |
| `EMAIL_SMTP_USERNAME` | `carpenter-ai@mailbox.org`             |
| `EMAIL_SMTP_PASSWORD` | your mailbox.org Email App Password    |

The file format is one `KEY=VALUE` per line.  Keep its mode `600` and
never commit it.  These resolve **platform-side** when a trusted handler
runs (via `CapabilityContext.secret`); they are never injected into the
untrusted executor, never embedded in a script, and never logged.

`EMAIL_IMAP_HOST` / `EMAIL_SMTP_HOST` also bind the egress
host for the capability grants, so the handler can never point egress
elsewhere.

There is no separate "authorize" step — unlike Gmail's OAuth flow, the
IMAP/SMTP backend is functional as soon as the eight credentials are
present in the `.env`.

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

## 6. mailbox.org behaviours you should know

- **Sent copies are filed explicitly.** mailbox.org does NOT auto-file
  SMTP-sent mail into `Sent` (Gmail's API does; raw SMTP does not).  This
  package handles it for you: after a successful send, the send flow
  dispatches `imap.append` to copy the outgoing message into `Sent`
  (marked `\Seen`).  The send receipt reports `sent_copy_filed: true`.
  If you ever see `sent_copy_filed: false`, the message WAS delivered —
  only the server-side Sent copy failed (e.g. transient IMAP error).
- **Spam goes to `Junk`, not INBOX.** mailbox.org's server-side spam
  filter files junk into the `Junk` folder.  Today this only matters if
  you explicitly search `Junk`.  When the inbound mail poller ships
  (v0.2.0, not built yet) it will need to decide whether to watch
  INBOX-only or INBOX+`Junk`; until then, no automatic inbound polling
  happens.

## 7. Troubleshooting

- **"expected_account is not configured"** — you haven't set
  `EMAIL_IMAP_USERNAME` (or `operator_email`).  The read/send tools
  fail closed without an expected account.
- **`imap.fetch failed` / `smtp.send failed`** — check the host/port and
  app password; confirm your provider allows IMAPS/SMTPS app-password
  access.  Errors never include the credential.
- **Recipient rejected at the chat boundary** — run
  `pkg_imap_trust_sender(...)` for that address.
