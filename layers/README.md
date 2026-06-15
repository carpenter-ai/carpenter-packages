# layers/ — shared code composed into capability packages

## Why layers exist

Carpenter's package manifest loader requires every declared asset
(`data_models.py`, `judge_handlers`, templates, `kb_articles`,
triggers) to physically live *inside* the package directory, and it
**rejects asset paths that escape the package root**. So shared,
security-critical code between two packages cannot be runtime-imported
for these assets — it must be **physically copied (composed)** into each
leaf package at build time.

A *layer* is a directory of shareable, backend-agnostic package assets.
A *leaf* package declares which layers it composes in its
`compose.yaml`, and the build tool ([`tools/compose.py`](../tools/README.md))
materialises the leaf by copying the layers in, then overlaying the
leaf's own files.

## `carpenter-email-core`

`carpenter-email-core` is the shared trust pipeline extracted out of the
`carpenter-gmail` package so a future `carpenter-imap-email` package can
reuse the exact same data models, JUDGEs, review templates, and KB
without copy-paste drift. It contains **byte-for-byte copies** of the
backend-agnostic assets currently shipped by `carpenter-gmail` (31
files):

* `data_models.py` — all trust-graduating dataclasses + sub-components.
* `judges.py` — every deterministic Python JUDGE handler.
* `handlers/triage_inbound.py` — the `email.received` subscription shim.
* `triggers/_index_common.py` — the shared `IndexTriggerBase`.
* `templates/<11 templates>/` — each `template.yaml` + `reviewer.txt`
  (read: simple-text, meeting-invite, order-confirmation; write: send,
  archive, mark-read, draft; triage; index: phase1, phase2,
  incremental).
* `kb/{trust-warning,attachments,style,inbound-triage,index}.md`.

Gmail-specific assets are deliberately **not** in the layer:
`tools.py`, `scripts.py`, `triggers/gmail_poll.py`, the thin
index-trigger wrappers (`email_index_phase1.py`, `email_index_phase2.py`,
`email_index_incremental.py`), `kb/{overview,policy-setup,search}.md`,
and `manifest.yaml`.

## Status of this work (additive scaffolding only)

This PR builds the composition tooling and the `carpenter-email-core`
layer, and **proves** the layer faithfully reproduces gmail's current
files (`python -m tools.compose verify packages/carpenter-gmail` →
`OK: 31 layer-contributed file(s) are byte-identical ...`). It does
**not** cut gmail over to consuming the layer. The gmail package still
ships its own copies of every layer file; that is expected. Verify-mode
is the standing CI drift guard that those copies stay identical.

## DEFERRED follow-up steps (intentionally NOT done here)

Each of these is a separate, reviewed step:

1. **Remove the now-duplicated files from `carpenter-gmail`'s tree** and
   generate them via `compose` at build/install time. (Today
   `python -m tools.compose compose packages/carpenter-gmail` correctly
   fails with a leaf-vs-layer collision — that failure is the signal
   this step is pending.)
2. **Wire the compose step into the package build/install pipeline** so
   leaf packages are materialised from their layers before the manifest
   loader runs.
3. **Extract the arc-tree builders from `carpenter-gmail/tools.py`** —
   the four `_create_*_arc_tree()` builders plus `_build_raw_message()`
   (the "parameterize-then-move" set) — into a
   `carpenter_email_core/arc_builders.py` in the layer.

### Couplings that will complicate the deferred cutover

Noticed while extracting the layer; flagged for the follow-up steps:

* **Hardcoded `carpenter_gmail` namespace in shared code.** Both
  `triggers/_index_common.py` and `handlers/triage_inbound.py` import
  from `carpenter_gmail.tools`, `carpenter_gmail.data_models`, and
  `carpenter_gmail.triggers.gmail_poll` (with relative-import
  fallbacks). For these files to be truly shared across `carpenter-gmail`
  and `carpenter-imap-email`, those imports must be parameterised (e.g.
  resolve the owning package at runtime, or inject the builders) — a
  layer file cannot statically name one consumer package. This is the
  core reason step 3 (move the arc builders into the layer) is
  "parameterize-*then*-move".
* **`_index_common.IndexTriggerBase._spawn_tick` calls
  `tools._create_index_arc_tree`** and `_drain_inflight` reads
  `data_models.EmailIndexFetchedBatch` — so the arc-builder extraction
  (step 3) and the trigger sharing are coupled and should land together
  or in a careful order.
* **`handlers/triage_inbound.py` calls `tools._create_triage_arc_tree`**
  and references `tools._create_read_arc_tree` in its docstring — same
  parameterisation requirement.
