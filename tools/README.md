# tools/ — package build tooling

## `compose.py` — layer composition for capability packages

Carpenter's package **manifest loader requires every declared asset**
(`data_models.py`, `judge_handlers`, templates, `kb_articles`,
triggers) to physically live *inside* the package directory; it rejects
asset paths that escape the package root. Shared code between packages
therefore cannot be runtime-imported for these assets — it must be
**physically copied (composed)** into each leaf package at build time.

`compose.py` performs that composition. See
[`../layers/README.md`](../layers/README.md) for the layer model and the
deferred cutover plan.

### `compose.yaml` format (in a leaf package)

```yaml
compose_from:
  - carpenter-email-core   # ordered list of layer names under layers/
overrides: []              # explicit allowlist of rel paths the leaf
                           # may override a layer-contributed file
```

* `compose_from`: ordered list of layer names. Layers live at
  `layers/<name>/` relative to the repo root.
* `overrides`: explicit allowlist of relative paths where a *later*
  source (a later layer, or the leaf itself) is permitted to overwrite
  a file already placed by an earlier source. Any collision **not** in
  this list is a hard error — drift stays loud.

### Algorithm

Starting from an empty composed tree:

1. For each layer in `compose_from` order, copy its files in.
2. Then copy the leaf's own files (everything except `compose.yaml`).

If a copy would overwrite a file already placed by an earlier source
**and** that relative path is not in `overrides`, composition fails with
an error naming the colliding path and both contributing sources.

### Entry points

Library:

```python
from tools.compose import compose, verify

out_dir = compose("packages/carpenter-gmail")        # -> composed tree path
result  = verify("packages/carpenter-gmail")          # -> VerifyResult
```

CLI:

```bash
# Materialise the composed tree (default: a temp dir; --out to choose).
python -m tools.compose compose packages/<leaf> [--out DIR]

# Drift guard / extraction-faithfulness proof: compose the layers and
# assert each layer-contributed file is byte-identical to the leaf's
# current on-disk copy. Exits non-zero on any mismatch.
python -m tools.compose verify packages/<leaf>
```

### `compose` vs `verify` (important during the pre-cutover phase)

* **`verify`** plans the *layers only* and compares each
  layer-contributed file against the leaf's current on-disk copy. It is
  designed for the present state where a leaf still physically ships
  byte-identical duplicates of every layer file — those duplicates are
  intentional and `verify` proves they have not drifted.
* **`compose`** plans the layers *and* overlays the leaf's own files,
  enforcing leaf-vs-layer collisions via `overrides`. This is the
  build/install-time shape for *after* the cutover, when a leaf no
  longer ships the duplicated files. Running `compose` against
  carpenter-gmail today is expected to fail with a collision — that
  failure is exactly the signal that the duplicate-removal cutover step
  has not happened yet.

### Tests

```bash
~/bin/run-tests tools/tests/test_compose.py -q
```

(Always use `~/bin/run-tests`, never bare `pytest`.) The suite covers
additive merge, undeclared-collision failure, override-allowed
collisions, layer ordering, verify pass/fail, config validation, and a
real-repo faithfulness check asserting `carpenter-email-core`
reproduces carpenter-gmail's 31 shared files byte-for-byte.

## `publish_release.py` — package-archive GitHub Releases publisher

Publishes each package version as a **GitHub Release asset** that the
Carpenter package-upgrade reconcile system fetches as the remote
pristine ("shipped") tree. The reconcile fetcher
(carpenter-linux's `ArchiveFetcher`) downloads the asset and
carpenter-core's `archive_cache.load_pristine_tree` **verifies it
against the recorded install root hash** (`installed_packages.hash` /
`installer.compute_package_hash`) before trusting a byte of it.

### Convention (publisher and fetcher MUST agree)

* Release **tag**:   `<name>-v<version>` (e.g. `carpenter-gmail-v0.7.0`)
* Release **asset**: `<name>-<version>.tar.gz`
* **Per-package** releases — versions are independent. Idempotent;
  **never** deletes old releases (historical assets must stay fetchable
  to reconstruct `shipped_old` for any installed version).

### Determinism contract (matches carpenter-core exactly)

The asset must expand to a tree that hashes identically to what the
installer measured. `publish_release.py` reproduces carpenter-core's
`archive_cache.archive_tree` **byte-for-byte**: it archives the
**on-disk `packages/<name>/` directory** (which is what
`install_package` copies as-is and `compute_package_hash` walks — core
has no `compose.yaml` awareness), applying the same ignore rules
(`installer._iter_files`), sorted POSIX member order, normalized
member metadata (`mtime/uid/gid=0`, `mode=0644`, `REGTYPE`), and
gzip `mtime=0`. For packages that declare layers it first runs
`compose verify` as a publish-time drift guard, refusing to ship a leaf
that has diverged from its layer. If carpenter-core's `archive_tree` /
`compute_package_hash` ever change, the mirrored copies here must change
in lockstep — the tests guard this.

### Usage

```bash
# Build the archive + print tag/asset/hash WITHOUT publishing:
python -m tools.publish_release carpenter-gmail --dry-run

# Build + publish (needs GITHUB_TOKEN with contents:write on the repo):
python -m tools.publish_release carpenter-gmail
```

### CI

The CI workflow runs the publisher on merge to `main` for any package
whose `manifest.yaml` version changed, plus a manual `workflow_dispatch`
taking a package name. It uses the built-in `GITHUB_TOKEN` with
`contents: write` (carpenter-packages is a **public** repo, so the
consumer fetcher downloads anonymously; only the publish job needs a
write token).

> **Activation note:** the workflow ships at
> `tools/ci/publish-package-archives.yml` and must be moved to
> `.github/workflows/publish-package-archives.yml` to take effect. It is
> parked outside `.github/workflows/` because the bot token that opened
> the PR lacks the GitHub `workflow` OAuth scope (GitHub refuses to push
> commits adding workflow files without it). A maintainer with that scope
> should relocate the file; its content is final.

### Tests

```bash
~/bin/run-tests tools/tests/test_publish_release.py -q
```

Mirrors carpenter-core's `test_archive_cache` determinism + round-trip
assertions (identical tree -> byte-identical archive; expanded archive
rehashes to the source root hash) plus naming, ignore-rule, and
real-repo build/round-trip checks.
