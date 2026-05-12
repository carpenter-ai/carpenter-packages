# carpenter-email — setup & first-use guide

This is the end-user guide for the `carpenter-email` package. It walks
you from a fresh Carpenter install through to "I can send and receive
mail through my chat agent". If you're a package developer looking for
the design / trust model, read [`README.md`](README.md) and the
[build plan](https://rainbow-forge.duckdns.org:3000/ben-harack/carpenter-core/src/branch/main/docs/2026-05-06_carpenter-email-build-plan.md)
in carpenter-core instead.

Phase 1 (the version that's shipped) gives you four things:

- **Search and read** your inbox through a structured-extract pipeline
  that never lets raw email bodies into the chat agent's context.
- **Send** email through Gmail with a per-recipient allowlist, a
  human-confirm prompt at the chat boundary, and an in-script check
  that the OAuth token actually belongs to the mailbox you think it
  does.
- **Authorize** a Gmail account once via OAuth.
- **Manage** the per-sender allowlist with `pkg_email_trust_sender`
  and `pkg_email_untrust_sender`.

What it deliberately does **not** do in Phase 1: poll your inbox for
new mail, read attachments, archive or mark messages read, save
drafts. Those are scheduled for Phase 1.5 (archive/mark-read/draft)
and Phase 3 (triggers and Pub/Sub).

---

## 1. Prerequisites

You need exactly one thing on the Google side before starting: a
Google Cloud OAuth 2.0 client (Web application type) with the Gmail
API enabled and a redirect URI pointing back at your Carpenter
instance.

### 1.1 Make a Google Cloud project

Sign in at <https://console.cloud.google.com/> with the Google account
whose mail you want Carpenter to read. (You can use an existing
project if you have one — just check the OAuth consent screen is
configured for your account.) Create a new project; give it any name
you'll recognise (e.g. "carpenter-email").

### 1.2 Enable the Gmail API

In the Cloud Console, open **APIs & Services → Library**, search for
"Gmail API", and click **Enable**. This is required for both the read
side (`gmail.googleapis.com/gmail/v1/users/me/messages`) and the send
side (`gmail.googleapis.com/gmail/v1/users/me/messages/send`).

### 1.3 Configure the OAuth consent screen

Open **APIs & Services → OAuth consent screen**. For a personal Google
account, choose **External**. Fill in the app name (e.g. "My
Carpenter"), your support email, and developer contact email. You can
leave most other fields blank.

On the **Scopes** screen, add three scopes:

- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/gmail.send`
- `https://www.googleapis.com/auth/userinfo.email`

(The `userinfo.email` scope is what lets the send pipeline verify the
OAuth token belongs to the mailbox you think it does — see §8 below.)

On the **Test users** screen, add your own Gmail address as a test
user. (You don't need to verify the app with Google as long as you're
the only user.)

### 1.4 Create the OAuth client

Open **APIs & Services → Credentials → Create Credentials → OAuth
client ID**. Choose **Application type: Web application**. Give it a
name ("carpenter-email").

Under **Authorized redirect URIs**, add one URI:

```
{public_base_url}/api/oauth/callback/carpenter-email
```

`{public_base_url}` is whatever URL your Carpenter instance is reachable
at from your browser — for a typical Pi setup that's something like
`https://rainbow-forge.duckdns.org:3080` (the session-platform port),
but it'll be different for your deployment. The path
(`/api/oauth/callback/carpenter-email`) is fixed and is what the
platform's generic OAuth callback handler listens on.

If you don't know your public base URL: open Carpenter in your
browser, look at the address bar, and copy everything before the
first single slash after the domain.

Click **Create**. Google shows you a **Client ID** and a **Client
secret**. Copy both — you'll paste them in step 2.

---

## 2. Install the package

In the Carpenter chat, ask the agent to install the package. The chat
agent calls the `install_package` tool internally:

> **You:** "Install the carpenter-email package."

The agent will run `install_package(name="carpenter-email")` and
should report something like:

> **Agent:** "Installed carpenter-email v0.1.0. Two allowlist
> entries (`gmail.googleapis.com`, `oauth2.googleapis.com`) need
> your approval; I've queued them for you. Also need OAuth
> credentials for Google — please paste the client_id / client_secret
> when prompted."

Behind the scenes the platform's package installer reads
`manifest.yaml` and:

1. Registers the four data-model dataclasses
   (`EmailReviewBriefing`, `EmailSimpleTextExtract`,
   `EmailMeetingInviteExtract`, `EmailOrderConfirmationExtract`) with
   the JUDGE-dispatch deserialiser.
2. Loads three arc templates (`email_read_simple_text`,
   `email_read_meeting_invite`, `email_read_order_confirmation`).
3. Wires the three deterministic JUDGE handlers.
4. Registers the seven chat tools (`pkg_email_authorize`,
   `pkg_email_search_emails`, `pkg_email_list_inbox`,
   `pkg_email_read_email`, `pkg_email_send_email`,
   `pkg_email_trust_sender`, `pkg_email_untrust_sender`).
5. Seeds four KB articles under `email/*`.
6. Presents two allowlist additions
   (`gmail.googleapis.com`, `oauth2.googleapis.com`) for your one-time
   confirmation.
7. Surfaces the OAuth credential-requirement UI so you can paste the
   client ID/secret.

When the credentials UI opens (a one-time link the agent will paste
into chat, or a page in the operator console depending on your
deployment), paste the **client_id** and **client_secret** from §1.4.
The platform stores them under `GMAIL_OAUTH_CLIENT_ID` and
`GMAIL_OAUTH_CLIENT_SECRET` in `.env`.

**Expected outcome:** "carpenter-email installed" + you've approved
both allowlist entries + you've supplied the OAuth client creds.
You're now ready to authorize a mailbox.

---

## 3. Authorize the Gmail account

The OAuth client you registered is just a "this app is allowed to
ask"; you still need to grant a specific Gmail account access to
Carpenter. That's a separate one-shot flow.

In chat:

> **You:** "Authorize my Gmail account."

The agent calls `pkg_email_authorize` and replies with a URL plus an
instructions blob:

> **Agent:** "Open this URL in a browser:
> `https://accounts.google.com/o/oauth2/v2/auth?...`. Sign in with
> the Google account whose mail you want me to read, grant the
> requested scopes, and the platform will write tokens to .env
> automatically."

Click the URL. Google walks you through the standard "Sign in" →
"Choose account" → "carpenter-email wants to access your Google
Account" consent screen. (If you set up the consent screen as a
testing app, Google will warn you the app is unverified — that's
expected; click "Continue" or "Advanced → Go to ... (unsafe)" because
you are the developer.)

After consent, Google redirects to your `{public_base_url}/api/oauth/
callback/carpenter-email`. The platform's callback handler:

- Validates the one-shot state token (defends against CSRF).
- Verifies the callback's `package_name` matches the in-flight authorize
  flow (defends against another package piggy-backing).
- Exchanges the code for an access token + refresh token.
- Calls `userinfo` to get the actual mailbox email address.
- Writes everything to `.env` under the `GMAIL_OAUTH_` prefix:
  `GMAIL_OAUTH_ACCESS_TOKEN`, `GMAIL_OAUTH_REFRESH_TOKEN`,
  `GMAIL_OAUTH_ACCOUNT_EMAIL`.

**Expected outcome:** the browser shows a success page; the chat
agent (on its next turn) reports the mailbox is connected. If you
return to chat and ask "what mailbox am I connected to?", the agent
can look it up via the `GMAIL_OAUTH_ACCOUNT_EMAIL` env key.

---

## 4. First use — search

Time to try the read pipeline. Ask a question that needs the inbox:

> **You:** "Search my inbox for anything about invoices from the last
> two weeks."

The agent calls `pkg_email_search_emails(query="newer_than:14d
invoice")`. (`query` uses standard
[Gmail search syntax](https://support.google.com/mail/answer/7190).)
You'll see something like:

> **Agent:** "Started a Gmail search for `newer_than:14d invoice`.
> The search arc is running; results will arrive shortly as
> JUDGE-approved extracts."

This is the read pipeline kicking off:

1. A PLANNER arc is created with an EXECUTOR child.
2. The EXECUTOR runs a pre-verified, package-author-audited Gmail
   search script (no agent-generated code) and writes the raw JSON
   result to an *untrusted* Resource.
3. When the search completes, you (or a follow-up planner step) ask
   the agent to read individual messages — see §5.

The chat agent never sees raw email bodies in this step; what it
gets is a list of message IDs.

---

## 5. First use — read

Ask the agent to read one of the search results. The agent picks one
of the three read templates based on what kind of message it is:

> **You:** "Read the top invoice result."

The agent calls
`pkg_email_read_email(provider_message_id="...", kind="order_confirmation")`.
That spins up a four-arc tree:

- **PLANNER (trusted)** — builds an `EmailReviewBriefing` from the
  sender allowlist snapshot + a static suspicious-keyword list, then
  hands off.
- **EXECUTOR (untrusted)** — runs a pre-verified Gmail fetch script,
  writes the raw message JSON to an untrusted Resource.
- **REVIEWER (constrained)** — reads the briefing + raw email under
  a static prompt with no KB access; emits a structured
  `EmailOrderConfirmationExtract` dataclass with sanitised
  body_summary + bounded fields (vendor, total, order_id, items).
- **JUDGE (deterministic Python)** — runs `judge_order_confirmation`
  in `judges.py`: rejects extracts with control chars, length
  overruns, schema-version mismatch, expected-account mismatch, or
  non-allowlisted email/URL literals. If approved, the extract
  Resource flips to `template_verdict='approved'` and the chat agent
  can `read_resource` it.

What you see in chat:

> **Agent:** "The top invoice is from `billing@acme-saas.com`,
> total $42.00, order id `INV-2026-005`, received yesterday at
> 14:22 UTC. Body summary: 'Thank you for your order. Your monthly
> subscription has renewed.' No suspicious flags."

What you don't see: the original HTML body, image trackers, prompt
injection attempts that may have been in the body, hidden Unicode in
the headers. Those are bounded and either rejected (by JUDGE) or
included only as length-capped, control-char-free, plaintext
summaries.

### What happens if the JUDGE rejects

Sometimes the REVIEWER produces an extract the JUDGE can't approve —
typically because the sender isn't in your `SecurityPolicies.email`
allowlist (so the `EmailPolicy` field literal validation fails before
the JUDGE handler even runs), or because the body contains control
characters the JUDGE bans. The chat agent reports a rejection reason:

> **Agent:** "Couldn't read that message: extract was rejected
> ('from_address `bob@unknown.example` not in email allowlist'). If
> you trust this sender, run `pkg_email_trust_sender` and ask again."

The arc tree is marked failed; no data crosses U→T. That's the
guarantee.

---

## 6. First use — send

Send works on a different pipeline (no U→T promotion — it's an
outbound effect, not an ingress), but with similar trust gates.

> **You:** "Send Alice a quick note saying I got the package."

The agent calls
`pkg_email_send_email(to=["alice@example.com"], subject="Got the
package", body="Just confirming I received it.")`. Three gates fire
in order:

1. **In-tool allowlist check.** Before the arc is even built, the
   chat tool checks every `to` address against
   `SecurityPolicies.email`. If any address isn't allowlisted, the
   tool returns an error immediately ("recipient
   `alice@example.com` is not in the email allowlist; use
   `pkg_email_trust_sender` to add"). No work is queued.
2. **Chat-boundary human confirm.** Because
   `pkg_email_send_email` declares `requires_user_confirm=True`, the
   chat agent shows you the full draft (recipients, subject, body)
   and asks "OK to send?" — you have to explicitly approve.
3. **In-script expected-account check.** Inside the untrusted
   EXECUTOR, the pre-verified send script calls Google's
   `userinfo` endpoint and verifies the OAuth token's actual mailbox
   matches the configured `GMAIL_OAUTH_ACCOUNT_EMAIL`. If a refresh
   token has been swapped under your feet, the send hard-fails with
   "expected-account check failed".

When all three pass, the EXECUTOR posts to
`gmail.googleapis.com/gmail/v1/users/me/messages/send` and reports
success back through the arc-completion notify channel:

> **Agent:** "Sent — Gmail accepted the message (status 200)."

---

## 7. Trust model — what the package will NOT do

A short list, because it's easier to use the package when you know
exactly where its limits are:

- **Never trusts a sender you didn't trust.** The
  `SecurityPolicies.email` allowlist is the only way an email's
  `from_address` becomes a valid `EmailPolicy` literal. Until you've
  called `pkg_email_trust_sender("...")`, every message from that
  sender will be rejected at JUDGE time. Phase 1 ships **zero**
  bootstrap senders.
- **Never reads attachments.** Phase 1 fetches `format=full` from
  Gmail and the REVIEWER summarises the text part only. Attachments
  are ignored. (Future: a separate attachment-handling design.)
- **Never polls your inbox.** Phase 1 only fetches messages when you
  ask. Inbound triggers (`email.message_received`) are Phase 3.
- **Never re-feeds extracted strings back into LLM context.** Subject
  lines, vendor names, location strings, etc. are header-derived and
  may contain hostile content. Phase 1 displays them to you with
  bounded length and control-char filtering; the agent will not stuff
  them into a follow-up arc goal or system prompt. (If you'd like
  it to act on those strings, do so explicitly in a new chat turn.)
- **Never sends to a non-allowlisted recipient.** The chat-tool
  allowlist check is belt-and-braces, and the `EmailPolicy`
  literal validation in `pkg_email_send_email`'s recipient list is
  what makes that check load-bearing.
- **Never bypasses the chat-boundary confirm** for sends or for
  allowlist mutations. Those always show you the full draft / address.

What it **does** rely on you to do:

- Be honest about which senders you trust (`pkg_email_trust_sender`
  is a one-way ratchet against the threat-model's "trusted senders
  accumulate" attack — keep the list short).
- Read displayed body summaries with appropriate skepticism. The
  JUDGE bans control characters and length overruns, but it does
  **not** semantically sanitise the text. A clever attacker can put
  social-engineering text in a 500-character summary; the package's
  job is to make sure that text reaches you as data you read, not
  as instructions to the assistant.

---

## 8. Troubleshooting

### "GMAIL_OAUTH_CLIENT_ID / GMAIL_OAUTH_CLIENT_SECRET are not set"

You skipped step 2's credential prompt. Re-run the install or use the
operator console's credentials UI to paste your Google Cloud OAuth
client ID/secret.

### Browser shows "Error 400: redirect_uri_mismatch"

The redirect URI in your Google Cloud OAuth client doesn't match what
the platform sent. Open Google Cloud Console → Credentials → your
OAuth client and confirm the **Authorized redirect URI** is exactly
`{public_base_url}/api/oauth/callback/carpenter-email` — including the
`https://`, including the port if non-default, no trailing slash.

### Browser shows "expected_account_email is not configured"

You haven't completed `pkg_email_authorize` yet. The chat tools are
deliberately fail-closed when the expected mailbox is unknown —
without it, the T1 envelope-recipient check can't be enforced.

### Send fails with "expected-account check failed: token belongs to
`other@gmail.com`, briefing said `you@gmail.com`"

The OAuth refresh token in `.env` belongs to a different Google
account than `GMAIL_OAUTH_ACCOUNT_EMAIL`. This usually means you
authorized one account, then re-ran `pkg_email_authorize` and picked
a different one mid-flow. Re-run `pkg_email_authorize` and pick the
same account you want listed as `GMAIL_OAUTH_ACCOUNT_EMAIL`. If you
deliberately want to change which mailbox is connected, you'll need
to update `GMAIL_OAUTH_ACCOUNT_EMAIL` in `.env` to match.

### Read fails with "from_address `bob@example.com` not in email allowlist"

This is the JUDGE rejecting an extract because the sender isn't in
`SecurityPolicies.email`. Three options:

- If you trust this sender: `pkg_email_trust_sender("bob@example.com")`
  and ask again. (Goes through human-confirm.)
- If you don't trust them but want to see who it's from: the rejection
  reason in chat already tells you the sender address and the subject
  field (the only fields available before the dataclass construction
  fails).
- If you think this was a mistake on the package side: open an issue
  — but it's almost always working as designed.

### Read fails with "body_summary contains control characters"

The REVIEWER tried to graduate an extract whose summary contained NUL
/ BEL / etc. The JUDGE banned it. You won't be able to read that
particular message through Phase 1's templates; this is by design
(those characters often signal display-corruption or terminal-injection
attempts). If you really need the message, fetch it manually through
your Gmail web client.

### `pkg_email_search_emails` returns but no follow-up read happens

Phase 1's search runs the EXECUTOR to produce a message-ID list but
the per-message read fan-out is currently manual: the chat agent
should pick a few of the returned IDs and call `pkg_email_read_email`
on each. Automatic fan-out is on the Phase 1.5 list.

---

## What's next

If you want more capabilities than Phase 1 ships:

- **Archive / mark read / draft** — Phase 1.5, see the build plan.
- **Inbound triggers** — Phase 3.
- **Attachments** — separate design, no schedule yet.

For the design rationale behind everything above (especially "why is
the read pipeline that elaborate?"), read the build plan in
carpenter-core at
`docs/2026-05-06_carpenter-email-build-plan.md`. The "Implementation
status" section at the bottom is the up-to-date map of what shipped vs
what's still designed-only.
