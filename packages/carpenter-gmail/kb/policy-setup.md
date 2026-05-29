# carpenter-email — allowlist setup

This package leans on Carpenter's `SecurityPolicies.email` allowlist
to decide which senders' addresses can flow through the trust system
and which recipients `pkg_email_send_email` will accept.

## What the allowlist actually does

* On the **read side**, every per-message extract dataclass has
  `from_address: EmailPolicy` and `to_addresses: tuple[EmailPolicy, ...]`.
  When the JUDGE-dispatch wrapper deserialises the extract, it
  validates each `EmailPolicy` literal against the global allowlist
  (D24 invariant I9).  An extract that names an unknown sender will
  fail validation and the JUDGE will return reject.

* On the **send side**, `pkg_email_send_email` validates each `to`
  address before it submits the EXECUTOR.  Unknown addresses are
  rejected with a clear error message that points the user at
  `pkg_email_trust_sender`.

## Adding entries

`pkg_email_trust_sender` is the only path.  It's a chat tool that:
1. Takes one email address.
2. Adds it to the persistent `policy_store` table.
3. Refreshes the in-memory `SecurityPolicies` singleton so future
   dataclass constructions see it.
4. Prompts the user for confirmation at the chat boundary.

The package ships **zero** sender allowlist proposals on day 1.
This is deliberate: bulk-importing a contacts list would be the
classic accumulation-attack vector (T9 in the threat model).  The
user adds one address at a time, with the chat-confirm prompt as
the audit log entry.

## Removing entries

`pkg_email_untrust_sender` removes one address.  Note that this does
NOT retroactively reject previously-approved extracts — those are
already trusted Resources.  It only affects future read pipelines
and future sends.

## Warning signs that an allowlist entry should be revoked

* The sender starts emailing about topics that look like impersonation
  attempts (account verification, password resets you didn't request,
  wire-transfer requests).
* You stop corresponding with that contact and don't expect future
  legitimate mail from them.
* Suspicious-keyword `flags` start appearing in their extracts.

## How the allowlist interacts with each tool

| Tool                       | Effect of allowlist                                                |
| --                         | --                                                                 |
| `pkg_email_list_inbox`     | Messages from non-allowlisted senders surface but their extracts will fail JUDGE because `from_address` is not in the allowlist. The chat agent only sees JUDGE-approved extracts. |
| `pkg_email_search_emails`  | Same — search results are still fetched, but extracts from non-allowlisted senders never graduate. |
| `pkg_email_read_email`     | Same as above for the single message. |
| `pkg_email_send_email`     | Each `to` must be in the allowlist; the call fails with a clear error otherwise. |
| `pkg_email_trust_sender`   | Adds an entry. Requires user confirmation. |
| `pkg_email_untrust_sender` | Removes an entry. Requires user confirmation. |
