# Email (IMAP/SMTP) — search

`pkg_imap_search_emails` runs an IMAP `SEARCH` through the trusted
`imap.search` capability verb and returns matching **UIDs** (most recent
first, capped at 25).  It does NOT return bodies — read a specific UID
with `pkg_imap_read_email` to graduate a typed extract.

## Query handling

The `query` argument is free text.  An empty query lists everything
(`SEARCH ALL`); a non-empty query is matched against message **TEXT**
(headers + body) server-side.  The trusted handler maps the query onto
an allowlisted IMAP search key and **quotes** the term, so the query
string can never inject into the IMAP command line.

The MVP exposes a single free-text path.  The trusted handler's
`criteria` API also supports an allowlisted set of structured keys
(`FROM`, `TO`, `SUBJECT`, `SINCE`, `BEFORE`, `UNSEEN`, `SEEN`,
`FLAGGED`, ...), which a future tool revision can surface for precise
filtering.

## No vector search yet

Unlike the Gmail backend, this package ships **no semantic / vector
index** in v0.1.0.  All search is the live IMAP `SEARCH` path above.
Vector search over the mailbox is deferred to v0.2.0.

## Typical flow

1. `pkg_imap_search_emails(query="invoice")` → list of UIDs.
2. For a UID of interest: `pkg_imap_read_email(provider_message_id="<uid>",
   kind="order_confirmation")` → JUDGE-approved extract.
3. Optionally `pkg_imap_archive_email` / `pkg_imap_mark_read_email`.
