# Email Semantic Resource Index

Carpenter-email (v0.6.0+) maintains a per-package vector index over
your Gmail mailbox.  The index lets `pkg_gmail_search_emails` answer
natural-language queries (e.g. "the message from Alice last week
about the conference travel reimbursement") without a Gmail API
round-trip.

This article is for the chat agent.  Use it when the user asks how
indexing works, why a search returned no hits, why indexing seems
slow, or how to pause / re-index.

## Architecture (one line)

Three PollableTriggers fire on a 60-second cadence; each tick
spawns one **PLANNER -> EXECUTOR -> REVIEWER -> JUDGE** arc tree;
the JUDGE graduates a structured `EmailIndexFetchedBatch`
dataclass; the trigger reads that on the next heartbeat and
performs the **embed + upsert** in trusted post-JUDGE context.

## Trust contract

* **Vector floats never leave the trigger thread**.  No vector data
  is ever serialised into a trusted-context string, an arc state
  value, or a chat-visible Resource.  (Package-internal invariant
  E1.)
* **The EXECUTOR never embeds**.  Embedding and `package.vectors`
  writes happen only after the JUDGE has approved a batch's
  metadata.  An untrusted EXECUTOR that lied about Gmail's
  response cannot poison the vector index — the JUDGE re-validates
  every field against the closed regexes / enums in
  `judges._sanitize_index_metadata`.
* **One package, one namespace**.  The carpenter-gmail vector store
  is bound to the package name at construction; no other package
  can read or write it (D24 I9 isolation, enforced by the
  `PackageVectorStore` loader).

## Three phases

| Phase | Purpose | Watermark | Per-tick cap |
|-------|---------|-----------|--------------|
| **Phase 1** | Backfill old mail, descending `internalDate` | oldest `internalDate` seen | 100 messages |
| **Phase 2** | Re-index bodies for a candidate list | candidate-list consumption | 50 messages |
| **incremental** | Pick up newly-arrived mail via `history.list` | Gmail `historyId` | 25 messages |

All three triggers share an `index_running` mutex in `package_state`
so only one tick runs at a time across the three phases.  This
keeps total Gmail-API quota use predictable.

## Watermarks and resumability

Every phase persists its watermark via CAS on
`package_state.cas()`.  If the daemon restarts mid-tick, the
in-flight arc state is preserved in `index_inflight_<phase>` and
the trigger drains it on the next check.  If the in-flight arc
ages out (>30 min), the trigger clears the flag and lets the next
tick spawn a fresh arc.

## Operator-visible controls

* `pkg_gmail_reindex` — wipes the namespace, resets all three
  watermarks.  Requires user confirm.  Use after changing the
  embedding model or to recover from a corrupt index.
* `pkg_gmail_reindex_pause` / `pkg_gmail_reindex_resume` — gate
  all three triggers via `index_paused` in `package_state`.
  Requires user confirm.

## Failure modes the trigger handles automatically

* **Model-identity mismatch**.  If the configured embedding model
  changes mid-flight, the existing vectors become incomparable
  with new ones.  The JUDGE flags this via
  `error_kind="model_identity_mismatch"` on an empty batch; the
  trigger pauses indexing and surfaces the reason via the chat
  agent's normal arc-completion notify path.  The user must run
  `pkg_gmail_reindex` to recover.
* **Expired Gmail history watermark** (>7 days stale).  Gmail
  returns 404 from `history.list`; the EXECUTOR catches that
  cleanly and emits `error_kind="history_expired"`.  The
  incremental trigger clears its watermark on receipt; Phase 1
  picks up the missed range on the next tick.
* **Transient embed errors**.  Per-entry; do not abort the whole
  batch.  The trigger writes a `sample_error_message` to the
  audit receipt and continues.

## When the chat agent should mention indexing

* If `pkg_gmail_search_emails` returns `backend: "keyword"` with
  `index_status.vector_count == 0`, say "the index is empty;
  Phase 1 backfill will populate it over the next few minutes."
* If `index_status.paused` is true, say "indexing is paused — run
  `pkg_gmail_reindex_resume` to continue."
* If the user complains that recent mail isn't found, check
  `index_status.incremental_ready` — if false, that's the cause.

Otherwise, indexing is silent operational machinery and need not
appear in conversation.
