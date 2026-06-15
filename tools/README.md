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
