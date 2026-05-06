# carpenter-email — overview

The `carpenter-email` package is the first real D24 capability
package: it lets the chat agent read and send email through a Gmail
account while obeying the Carpenter trust invariants.  The chat
agent never sees raw inbound email bodies — every read goes through
a pipeline that produces a structured, JUDGE-validated extract.

## What this package gives you

* **Inbox reading** — `pkg_email_list_inbox`, `pkg_email_search_emails`,
  `pkg_email_read_email`.  Each fans out an arc tree
  (PLANNER -> EXECUTOR -> REVIEWER -> JUDGE) per matching message.
  Once the JUDGE approves, the chat agent can `read_resource` on a
  trusted `EmailSimpleTextExtract`, `EmailMeetingInviteExtract`, or
  `EmailOrderConfirmationExtract` dataclass.

* **Sending** — `pkg_email_send_email` (chat-confirm + allowlist-checked
  recipients + in-script expected-account check).

* **Allowlist management** — `pkg_email_trust_sender` and
  `pkg_email_untrust_sender` add or remove `EmailPolicy` entries.
  Both require user confirmation at the chat boundary.

* **OAuth bootstrap** — `pkg_email_authorize` returns a one-time
  Google sign-in URL.  Tokens are stored in the platform `.env`
  under the `GMAIL_OAUTH_*` prefix.

## Trust architecture

```
PLANNER (trusted)
  -> writes EmailReviewBriefing (born-trusted Resource)
EXECUTOR (untrusted)
  -> Gmail API users.messages.get -> raw_email Resource (encrypted, untrusted)
REVIEWER (constrained, static prompt, no KB)
  -> reads briefing + raw_email -> Email{kind}Extract (pending verdict)
JUDGE (deterministic Python — package's judges.py)
  -> validates structural fields + policy literals
  -> flips the extract Resource's template_verdict to approved/rejected
Trusted parent / chat agent reads the approved extract via read_resource
```

The chat agent NEVER sees the raw email body.  It sees a typed
extract with explicit, length-bounded fields.  Suspicious content is
surfaced as `flags`, never as instructions.

## Threat model in one paragraph

Inbound email is **untrusted data** (D24 invariant I1).  Attackers
can attempt prompt injection in bodies (T1), spoof senders that
happen to be in the allowlist (T2), embed phishing links (T3), or
craft replies into existing trusted threads (T10).  Each one is
neutralised by a different layer: REVIEWER's static prompt and lack
of KB access blunts T1 and T6; the global `SecurityPolicies.email`
allowlist plus per-extract `EmailPolicy` validation blunts T2; URLs
are surfaced as a separate, allowlist-validated `extracted_urls`
list with the body text scrubbed of literal links (T3); each message
is reviewed independently with no thread-level trust caching (T10).

## What's in Phase 1

* Three read templates: `email_read_simple_text`,
  `email_read_meeting_invite`, `email_read_order_confirmation`.
* `pkg_email_send_email` with chat-confirm + allowlist + expected-
  account check.
* OAuth via Google's authorization-code flow with `gmail.readonly`,
  `gmail.send`, `userinfo.email` scopes.
* Two allowlist proposals at install time: domain entries for
  `gmail.googleapis.com` and `oauth2.googleapis.com`.
* Zero sender-allowlist proposals on day 1 — the user populates the
  email allowlist one entry at a time via `pkg_email_trust_sender`,
  defending against the accumulation-attack threat (T9).

## Reading more

* `kb/email/policy-setup.md` — how the allowlist works in practice.
* `kb/email/trust-warning.md` — guidance for the chat agent on
  handling email-derived information safely.
* `kb/email/style.md` — the user's preferred email-writing style for
  outbound composition.
