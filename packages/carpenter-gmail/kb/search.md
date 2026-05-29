# Email Search (`pkg_email_search_emails`)

`pkg_email_search_emails` has two backends:

* **Vector** — semantic search over the carpenter-email
  `PackageVectorStore`.  No Gmail API round-trip.  Fast.  Requires
  the index to be populated (see `email/index` KB article).
* **Keyword** — fans out an EXECUTOR that calls
  `gmail.users.messages.list` with the user's query string, then
  fans out a `email_read_simple_text` arc per matching message.
  Slower but works without indexing and supports Gmail's full
  search-operator syntax.

## Auto-selection rules

When the tool is called with `backend: "auto"` (the default):

1. If the query contains a Gmail-specific operator
   (`from:`, `to:`, `subject:`, `newer_than:`, `in:`, `label:`,
   etc), keyword is chosen.  Gmail operators are precise; the
   index is approximate.
2. If Phase 1 backfill is not yet `index_status.phase1_complete`,
   keyword is chosen (the index is too sparse to be useful).
3. Otherwise, vector is chosen.

Pass `backend: "vector"` or `backend: "keyword"` to force one or
the other.

## Response shape

Both backends include an `index_status` snapshot in the response so
the chat agent can phrase honest answers about indexing progress.
Vector responses include the hit list inline; keyword responses
return a parent `arc_id` and the per-message extracts arrive via
the standard arc-completion notify channel.

## Limits

* `max_results`: 1-25.
* Vector queries are clamped to 8000 characters before embedding.
* Neither backend ever surfaces raw email body content to the chat
  agent.  Read access still goes through the
  `email_read_simple_text` / `meeting_invite` / `order_confirmation`
  pipelines.
