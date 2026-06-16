#!/usr/bin/env python3
"""Compose a Carpenter capability package from shared layers + leaf files.

Carpenter's package manifest loader requires every declared asset
(``data_models.py``, ``judge_handlers``, templates, ``kb_articles``,
triggers) to physically live *inside* the package directory; it rejects
asset paths that escape the package root.  Shared code between packages
therefore cannot be runtime-imported for these assets — it must be
**physically copied (composed)** into each leaf package at build time.

This tool does that composition.  A *leaf* package declares, in a
``compose.yaml`` at its root, an ordered list of *layers* to pull in.
Layers live at ``layers/<name>/`` relative to the repository root.  The
tool builds a composed tree by, in order:

1. For each layer in ``compose_from`` order, copying every file in the
   layer into the (initially empty) composed tree.
2. Finally copying the leaf's own files (everything in the leaf dir
   except ``compose.yaml`` itself).

If a copy would overwrite a file already placed by an *earlier* source,
that is a collision.  Collisions are an error **unless** the colliding
relative path is in the leaf's ``overrides`` allowlist, in which case the
later source is permitted to win.  This makes accidental drift between a
layer and a leaf loud, while still allowing a leaf to deliberately
specialise a layer file.

Two entry points are provided:

* :func:`compose` — materialise the composed tree into an output dir.
* :func:`verify` — compose, then assert the result is byte-identical to
  the leaf dir's *current* on-disk content for every file the layers
  contribute.  Used to prove a faithful extraction and, later, as a CI
  drift guard.

CLI::

    python -m tools.compose compose <leaf_dir> [--out DIR]
    python -m tools.compose verify  <leaf_dir>

``verify`` exits non-zero on any mismatch.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency hint
    raise SystemExit(
        "PyYAML is required for the compose tool: pip install pyyaml"
    ) from exc


COMPOSE_FILENAME = "compose.yaml"
LAYERS_DIRNAME = "layers"


class ComposeError(RuntimeError):
    """Raised on any composition failure (bad config, collision, etc.)."""


@dataclass
class ComposeSpec:
    """Parsed ``compose.yaml`` for one leaf package."""

    compose_from: list[str] = field(default_factory=list)
    overrides: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> "ComposeSpec":
        if not path.is_file():
            raise ComposeError(f"no {COMPOSE_FILENAME} at {path}")
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ComposeError(f"{path}: top-level YAML must be a mapping")
        compose_from = data.get("compose_from") or []
        overrides = data.get("overrides") or []
        if not isinstance(compose_from, list) or not all(
            isinstance(x, str) for x in compose_from
        ):
            raise ComposeError(
                f"{path}: 'compose_from' must be a list of layer-name strings"
            )
        if not isinstance(overrides, list) or not all(
            isinstance(x, str) for x in overrides
        ):
            raise ComposeError(
                f"{path}: 'overrides' must be a list of relative-path strings"
            )
        # Normalise override paths to posix form for stable comparison.
        overrides = [_normalise_rel(x) for x in overrides]
        return cls(compose_from=list(compose_from), overrides=overrides)


def _normalise_rel(rel: str) -> str:
    """Normalise a relative path to forward-slash posix form."""
    return Path(rel).as_posix()


def _iter_files(root: Path, *, exclude: Iterable[str] = ()) -> list[str]:
    """Return sorted relative posix paths of all files under ``root``.

    ``exclude`` is a set of top-level filenames to skip.
    """
    exclude_set = set(exclude)
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        # Only exclude when the excluded name is the *whole* relative
        # path (i.e. a top-level file), so a nested file that happens to
        # share the name is not dropped.
        if rel in exclude_set:
            continue
        out.append(rel)
    return out


def _repo_root_from_leaf(leaf_dir: Path) -> Path:
    """Find the repo root (the dir containing ``layers/``) above a leaf.

    Walks up from ``leaf_dir`` looking for a sibling ``layers``
    directory.  Falls back to ``leaf_dir.parents[1]`` (``packages/<x>``
    -> repo root) if not found, which keeps the common layout working.
    """
    for candidate in [leaf_dir, *leaf_dir.parents]:
        if (candidate / LAYERS_DIRNAME).is_dir():
            return candidate
    # Best-effort fallback for the canonical packages/<name>/ layout.
    if len(leaf_dir.parents) >= 2:
        return leaf_dir.parents[1]
    raise ComposeError(
        f"could not locate a '{LAYERS_DIRNAME}/' directory above {leaf_dir}"
    )


def _resolve_layer_dir(repo_root: Path, layer_name: str) -> Path:
    layer_dir = repo_root / LAYERS_DIRNAME / layer_name
    if not layer_dir.is_dir():
        raise ComposeError(
            f"layer '{layer_name}' not found at {layer_dir}"
        )
    return layer_dir


@dataclass
class CompositionPlan:
    """The set of (source-label, rel-path, abs-src) placements, in order."""

    # rel-path -> (source_label, absolute source file)
    placements: dict[str, tuple[str, Path]] = field(default_factory=dict)
    # rel paths contributed by layers (i.e. not by the leaf itself)
    layer_paths: set[str] = field(default_factory=set)


def plan_composition(
    leaf_dir: Path,
    *,
    repo_root: Path | None = None,
    include_leaf: bool = True,
) -> CompositionPlan:
    """Build the ordered placement plan without writing anything.

    Args:
        leaf_dir: leaf package dir (contains ``compose.yaml``).
        repo_root: override repo-root detection (mainly for tests).
        include_leaf: if ``True`` (build/compose mode), the leaf's own
            files overlay the layers and a leaf-vs-layer collision is
            enforced via ``overrides``.  If ``False`` (verify mode),
            only the layers are planned; the leaf's files are NOT
            overlaid.  This matters during the pre-cutover phase where
            a leaf still physically ships byte-identical *copies* of
            every layer file: those duplicates are intentional and
            should be compared against (verify), not flagged as build
            collisions.

    Collisions *between layers* are always enforced regardless of
    ``include_leaf``.

    Raises:
        ComposeError: on an undeclared collision (or bad config).
    """
    leaf_dir = leaf_dir.resolve()
    if repo_root is None:
        repo_root = _repo_root_from_leaf(leaf_dir)
    spec = ComposeSpec.from_file(leaf_dir / COMPOSE_FILENAME)
    overrides = set(spec.overrides)

    plan = CompositionPlan()

    # 1) Layers, in declared order.
    for layer_name in spec.compose_from:
        layer_dir = _resolve_layer_dir(repo_root, layer_name)
        label = f"layer:{layer_name}"
        for rel in _iter_files(layer_dir):
            if rel in plan.placements:
                prev_label, _ = plan.placements[rel]
                if rel not in overrides:
                    raise ComposeError(
                        f"collision on '{rel}': contributed by both "
                        f"{prev_label} and {label}; add it to "
                        f"'overrides' in {COMPOSE_FILENAME} to allow "
                        f"the later source to win"
                    )
            plan.placements[rel] = (label, layer_dir / rel)
            plan.layer_paths.add(rel)

    if not include_leaf:
        return plan

    # 2) The leaf's own files (everything except compose.yaml).
    label = "leaf"
    for rel in _iter_files(leaf_dir, exclude={COMPOSE_FILENAME}):
        if rel in plan.placements:
            prev_label, _ = plan.placements[rel]
            if rel not in overrides:
                raise ComposeError(
                    f"collision on '{rel}': contributed by both "
                    f"{prev_label} and {label}; add it to 'overrides' "
                    f"in {COMPOSE_FILENAME} to allow the leaf to win"
                )
        plan.placements[rel] = (label, leaf_dir / rel)

    return plan


def compose(
    leaf_dir: str | Path,
    *,
    out_dir: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> Path:
    """Materialise the composed package tree.

    Args:
        leaf_dir: Path to the leaf package (contains ``compose.yaml``).
        out_dir: Where to write the composed tree.  If ``None``, a fresh
            temp dir is created and returned (caller owns cleanup).
        repo_root: Override repo-root detection (mainly for tests).

    Returns:
        The path to the composed tree directory.
    """
    leaf_path = Path(leaf_dir).resolve()
    root = Path(repo_root).resolve() if repo_root is not None else None
    plan = plan_composition(leaf_path, repo_root=root)

    if out_dir is None:
        out_path = Path(tempfile.mkdtemp(prefix="compose-"))
    else:
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)

    for rel, (_label, src) in sorted(plan.placements.items()):
        dest = out_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return out_path


@dataclass
class VerifyResult:
    ok: bool
    checked: int
    mismatches: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return (
                f"OK: {self.checked} layer-contributed file(s) are "
                f"byte-identical to the leaf's current on-disk content."
            )
        lines = ["FAIL: composed tree diverges from leaf on-disk content."]
        for m in self.missing:
            lines.append(f"  missing on leaf: {m}")
        for m in self.mismatches:
            lines.append(f"  byte mismatch:   {m}")
        return "\n".join(lines)


def verify(
    leaf_dir: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> VerifyResult:
    """Compose, then assert every layer-contributed file matches the
    leaf dir's current on-disk content byte-for-byte.

    Only files that a *layer* contributes are checked — the leaf's own
    files trivially match themselves, and the point of verification is
    to prove the layer faithfully reproduces what the leaf currently
    ships.
    """
    leaf_path = Path(leaf_dir).resolve()
    root = Path(repo_root).resolve() if repo_root is not None else None
    # Plan layers only: the leaf's current on-disk copies are what we
    # are verifying *against*, so we must not overlay (and thereby
    # collide with) them.
    plan = plan_composition(leaf_path, repo_root=root, include_leaf=False)

    mismatches: list[str] = []
    missing: list[str] = []
    checked = 0
    for rel in sorted(plan.layer_paths):
        _label, src = plan.placements[rel]
        leaf_file = leaf_path / rel
        if not leaf_file.is_file():
            missing.append(rel)
            continue
        checked += 1
        if not filecmp.cmp(src, leaf_file, shallow=False):
            mismatches.append(rel)
    ok = not mismatches and not missing
    return VerifyResult(
        ok=ok, checked=checked, mismatches=mismatches, missing=missing
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_compose = sub.add_parser(
        "compose", help="materialise the composed package tree"
    )
    p_compose.add_argument("leaf_dir", help="leaf package dir (has compose.yaml)")
    p_compose.add_argument(
        "--out", default=None, help="output dir (default: a temp dir)"
    )

    p_verify = sub.add_parser(
        "verify",
        help="compose and assert layer files match the leaf on-disk content",
    )
    p_verify.add_argument("leaf_dir", help="leaf package dir (has compose.yaml)")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "compose":
            out = compose(args.leaf_dir, out_dir=args.out)
            print(out)
            return 0
        if args.cmd == "verify":
            result = verify(args.leaf_dir)
            print(result.summary())
            return 0 if result.ok else 1
    except ComposeError as exc:
        print(f"compose error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
