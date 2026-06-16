# Email (IMAP/SMTP) — policy & credential setup

## Credentials the operator supplies at install

This package declares one `kind: env` credential requirement with the
prefix `IMAP_EMAIL`.  At install the operator is prompted for eight
values:

| Env var                    | Meaning                              |
|----------------------------|--------------------------------------|
| `IMAP_EMAIL_IMAP_HOST`     | IMAP server hostname (IMAPS / 993)   |
| `IMAP_EMAIL_IMAP_PORT`     | IMAP port (993)                      |
| `IMAP_EMAIL_IMAP_USERNAME` | IMAP login (usually the address)     |
| `IMAP_EMAIL_IMAP_PASSWORD` | IMAP app password                    |
| `IMAP_EMAIL_SMTP_HOST`     | SMTP server hostname (SMTPS / 465)   |
| `IMAP_EMAIL_SMTP_PORT`     | SMTP port (465)                      |
| `IMAP_EMAIL_SMTP_USERNAME` | SMTP login (usually the address)     |
| `IMAP_EMAIL_SMTP_PASSWORD` | SMTP app password                    |

These resolve **platform-side** via `CapabilityContext.secret(...)` when
a trusted handler runs.  They are never injected into the untrusted
EXECUTOR.

`IMAP_EMAIL_IMAP_HOST` and `IMAP_EMAIL_SMTP_HOST` also supply the egress
host for the capability grants (the manifest's `grant.host_from`), so
the operator confirms a concrete `host:port` at install and the handler
can never point egress elsewhere.

## Capability confirmation (platform-level trust)

Because the package declares `platform_capabilities`, installing it
grants **platform-level trust**: the `imap.*` / `smtp.send` handlers run
parent-side with egress + credentials.  The operator confirms each verb
+ grant interactively at install.

## Allowlist

The package proposes two domain allowlist entries (`imap.mailbox.org`,
`smtp.mailbox.org`).  The production provider + account are **confirmed**
(mailbox.org, `carpenter-ai@mailbox.org`); the operator still confirms
each entry at install via the standard human-confirm flow — nothing is
trusted silently.

The email **sender/recipient** allowlist (`SecurityPolicies.email`)
ships **empty**.  The operator/user adds entries one at a time via
`pkg_imap_trust_sender` (human-confirmed) — this is the ratchet against
the "trusted senders accumulate" threat.  Until a sender is trusted,
their messages fail `EmailPolicy` validation at JUDGE time; until a
recipient is trusted, `pkg_imap_send_email` refuses it at the chat
boundary.
