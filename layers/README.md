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
reuse the exact same data models, JUDGEs, review templates, KB, and
arc-tree builders without copy-paste drift. It is the **canonical,
parameterised source** for the backend-agnostic email assets (32 files):

* `data_models.py` — all trust-graduating dataclasses + sub-components.
* `judges.py` — every deterministic Python JUDGE handler.
* `arc_builders.py` — the shared PLANNER → EXECUTOR → REVIEWER → JUDGE
  arc-tree builders (`_create_{read,triage,write,index}_arc_tree`,
  `_build_raw_message`, and the `EXTRACT_KIND_BY_TEMPLATE` /
  `_WRITE_EXTRACT_KIND_BY_TEMPLATE` maps). Backend-specifics (the
  pre-verified EXECUTOR script and the audit `source_descriptor`
  prefix) are **arguments**, not imports — the module names no backend.
* `handlers/triage_inbound.py` — the `email.received` subscription shim.
  It imports `_create_triage_arc_tree` from the owning package's
  `tools` module via a relative `from ..tools import …`, so the shim is
  backend-agnostic while each backend's `tools` wrapper binds its own
  fetch script.
* `triggers/_index_common.py` — the shared `IndexTriggerBase`. It calls
  `arc_builders._create_index_arc_tree` and resolves the three
  backend-specific decisions via subclass hooks:
  `account_email_state_key` (the `package_state` key holding the
  authorised mailbox address), `raw_source_prefix`, and
  `index_template_meta(template_name) -> (extract_kind, script)`.
* `templates/<11 templates>/` — each `template.yaml` + `reviewer.txt`
  (read: simple-text, meeting-invite, order-confirmation; write: send,
  archive, mark-read, draft; triage; index: phase1, phase2,
  incremental).
* `kb/{trust-warning,attachments,style,inbound-triage,index}.md`.

Gmail-specific assets are deliberately **not** in the layer:
`tools.py` (now thin wrappers binding Gmail's scripts + source prefix
into the shared builders), `scripts.py`, `triggers/gmail_poll.py`,
`triggers/_gmail_index_base.py` (the Gmail base supplying the three
`IndexTriggerBase` hooks), the thin index-trigger wrappers
(`email_index_phase1.py`, `email_index_phase2.py`,
`email_index_incremental.py`), `kb/{overview,policy-setup,search}.md`,
and `manifest.yaml`.

## Integration model: committed composed copies + verify-mode drift guard

Carpenter's `install_package` copies a package's source dir as-is and
the registry scans `packages/<name>/`, so every leaf package KEEPS a
complete, installable **composed copy** of each layer file in its own
directory (we do NOT delete gmail's copies). The layer is the single
**edit-source**: change a shared file in `layers/carpenter-email-core/`,
then re-sync the leaf copies (via `tools/compose.py` build-mode, or by
copying). `python -m tools.compose verify packages/<leaf>` is the
standing drift guard — it asserts every layer-contributed file is
byte-identical to the leaf's committed copy:

```
python -m tools.compose verify packages/carpenter-gmail
# OK: 32 layer-contributed file(s) are byte-identical ...
```

No install/registry change is needed for this model.

## History

The initial extraction (PR #2) created the layer as **byte-identical
copies** of gmail's then-current backend-agnostic files (31 files) and
proved faithfulness with verify-mode. That byte-identical-to-gmail
proof applied **only at that initial extraction**. The cutover PR then
**parameterised** the layer (dropped the `carpenter_gmail.*`
absolute-import branches in `_index_common.py` / `triage_inbound.py`,
abstracted the `gmail_poll._KEY_ACCOUNT_EMAIL` coupling behind the
`account_email_state_key` hook, and moved the arc-tree builders into
`arc_builders.py` with backend-specifics turned into arguments/hooks),
and cut `carpenter-gmail` over to consume it with zero behavior change.
From the cutover onward the layer is the canonical parameterised source
and the leaf copies are regenerated from it; verify-mode keeps them in
lockstep.
