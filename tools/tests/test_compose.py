"""Unit tests for the package composition tool (``tools/compose.py``).

Covered behaviours:

* additive merge across layers + leaf works;
* an undeclared collision between two sources raises ``ComposeError``;
* an ``overrides``-declared collision is allowed (later source wins);
* layer ordering is respected (earlier layer wins ties, modulo
  overrides);
* ``verify`` passes when composed == current on-disk and fails on a
  deliberate mismatch;
* the real ``carpenter-email-core`` layer faithfully reproduces the
  current ``carpenter-gmail`` files (the extraction-faithfulness proof
  doubles as a CI drift guard).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``tools`` importable when tests run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import compose as compose_mod  # noqa: E402
from tools.compose import (  # noqa: E402
    ComposeError,
    compose,
    plan_composition,
    verify,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a synthetic repo skeleton: returns (repo_root, leaf_dir)."""
    repo = tmp_path / "repo"
    (repo / "layers").mkdir(parents=True)
    leaf = repo / "packages" / "leaf"
    leaf.mkdir(parents=True)
    return repo, leaf


# ── additive merge ──────────────────────────────────────────────────


def test_additive_merge_layer_then_leaf(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    _write(repo / "layers" / "core" / "shared.py", "shared\n")
    _write(repo / "layers" / "core" / "kb" / "a.md", "kb-a\n")
    _write(leaf / "tools.py", "leaf-tools\n")
    _write(leaf / "compose.yaml", "compose_from:\n  - core\noverrides: []\n")

    out = compose(leaf, out_dir=tmp_path / "out", repo_root=repo)

    assert (out / "shared.py").read_text() == "shared\n"
    assert (out / "kb" / "a.md").read_text() == "kb-a\n"
    assert (out / "tools.py").read_text() == "leaf-tools\n"
    # compose.yaml is never copied into the composed tree.
    assert not (out / "compose.yaml").exists()


# ── undeclared collision raises ─────────────────────────────────────


def test_undeclared_collision_raises(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    _write(repo / "layers" / "core" / "data_models.py", "layer-version\n")
    _write(leaf / "data_models.py", "leaf-version\n")
    _write(leaf / "compose.yaml", "compose_from:\n  - core\noverrides: []\n")

    with pytest.raises(ComposeError) as exc:
        plan_composition(leaf, repo_root=repo)
    msg = str(exc.value)
    assert "data_models.py" in msg
    assert "layer:core" in msg and "leaf" in msg


def test_undeclared_collision_between_two_layers_raises(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    _write(repo / "layers" / "a" / "x.py", "a\n")
    _write(repo / "layers" / "b" / "x.py", "b\n")
    _write(leaf / "compose.yaml", "compose_from:\n  - a\n  - b\noverrides: []\n")

    with pytest.raises(ComposeError) as exc:
        plan_composition(leaf, repo_root=repo)
    assert "x.py" in str(exc.value)
    assert "layer:a" in str(exc.value) and "layer:b" in str(exc.value)


# ── overrides-declared collision allowed ────────────────────────────


def test_override_allows_leaf_to_win(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    _write(repo / "layers" / "core" / "data_models.py", "layer-version\n")
    _write(leaf / "data_models.py", "leaf-version\n")
    _write(
        leaf / "compose.yaml",
        "compose_from:\n  - core\noverrides:\n  - data_models.py\n",
    )

    out = compose(leaf, out_dir=tmp_path / "out", repo_root=repo)
    # leaf is the later source, so it wins.
    assert (out / "data_models.py").read_text() == "leaf-version\n"


def test_override_allows_later_layer_to_win(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    _write(repo / "layers" / "a" / "x.py", "a\n")
    _write(repo / "layers" / "b" / "x.py", "b\n")
    _write(
        leaf / "compose.yaml",
        "compose_from:\n  - a\n  - b\noverrides:\n  - x.py\n",
    )

    out = compose(leaf, out_dir=tmp_path / "out", repo_root=repo)
    # b is declared after a, so the later layer wins under override.
    assert (out / "x.py").read_text() == "b\n"


# ── layer ordering ──────────────────────────────────────────────────


def test_layer_ordering_earlier_wins_without_override(tmp_path: Path) -> None:
    """With no override, two layers sharing a path is a collision; but
    distinct paths across ordered layers all land, and the leaf overlays
    last."""
    repo, leaf = _make_repo(tmp_path)
    _write(repo / "layers" / "a" / "only_a.py", "a\n")
    _write(repo / "layers" / "b" / "only_b.py", "b\n")
    _write(leaf / "leaf.py", "leaf\n")
    _write(leaf / "compose.yaml", "compose_from:\n  - a\n  - b\noverrides: []\n")

    plan = plan_composition(leaf, repo_root=repo)
    assert plan.placements["only_a.py"][0] == "layer:a"
    assert plan.placements["only_b.py"][0] == "layer:b"
    assert plan.placements["leaf.py"][0] == "leaf"
    assert {"only_a.py", "only_b.py"} <= plan.layer_paths
    assert "leaf.py" not in plan.layer_paths


def test_layer_order_is_respected_in_override(tmp_path: Path) -> None:
    """Reversing compose_from order flips which layer wins an override."""
    repo, leaf = _make_repo(tmp_path)
    _write(repo / "layers" / "a" / "x.py", "a\n")
    _write(repo / "layers" / "b" / "x.py", "b\n")
    _write(
        leaf / "compose.yaml",
        "compose_from:\n  - b\n  - a\noverrides:\n  - x.py\n",
    )
    out = compose(leaf, out_dir=tmp_path / "out", repo_root=repo)
    # Now a is declared last, so a wins.
    assert (out / "x.py").read_text() == "a\n"


# ── verify mode ─────────────────────────────────────────────────────


def test_verify_passes_when_composed_equals_current(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    _write(repo / "layers" / "core" / "shared.py", "shared\n")
    _write(repo / "layers" / "core" / "kb" / "a.md", "kb-a\n")
    # The leaf currently ships byte-identical copies (the pre-cutover
    # state this tool is meant to prove faithful).
    _write(leaf / "shared.py", "shared\n")
    _write(leaf / "kb" / "a.md", "kb-a\n")
    _write(leaf / "tools.py", "leaf-only\n")
    _write(leaf / "compose.yaml", "compose_from:\n  - core\noverrides: []\n")

    result = verify(leaf, repo_root=repo)
    assert result.ok, result.summary()
    assert result.checked == 2  # only layer-contributed files are checked
    assert not result.mismatches and not result.missing


def test_verify_fails_on_byte_mismatch(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    _write(repo / "layers" / "core" / "shared.py", "shared-LAYER\n")
    _write(leaf / "shared.py", "shared-LEAF-DRIFTED\n")
    _write(leaf / "compose.yaml", "compose_from:\n  - core\noverrides: []\n")

    result = verify(leaf, repo_root=repo)
    assert not result.ok
    assert "shared.py" in result.mismatches


def test_verify_reports_missing_leaf_file(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    _write(repo / "layers" / "core" / "shared.py", "shared\n")
    _write(repo / "layers" / "core" / "extra.py", "extra\n")
    # Leaf only ships one of the two layer files.
    _write(leaf / "shared.py", "shared\n")
    _write(leaf / "compose.yaml", "compose_from:\n  - core\noverrides: []\n")

    result = verify(leaf, repo_root=repo)
    assert not result.ok
    assert "extra.py" in result.missing


# ── config validation ───────────────────────────────────────────────


def test_missing_compose_yaml_raises(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    with pytest.raises(ComposeError):
        plan_composition(leaf, repo_root=repo)


def test_unknown_layer_raises(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    _write(leaf / "compose.yaml", "compose_from:\n  - nope\noverrides: []\n")
    with pytest.raises(ComposeError) as exc:
        plan_composition(leaf, repo_root=repo)
    assert "nope" in str(exc.value)


def test_bad_compose_from_type_raises(tmp_path: Path) -> None:
    repo, leaf = _make_repo(tmp_path)
    _write(leaf / "compose.yaml", "compose_from: not-a-list\noverrides: []\n")
    with pytest.raises(ComposeError):
        plan_composition(leaf, repo_root=repo)


# ── real-repo faithfulness proof / drift guard ──────────────────────


def test_gmail_layer_extraction_is_faithful() -> None:
    """The carpenter-email-core layer reproduces carpenter-gmail's
    current on-disk files byte-for-byte.  This is the extraction proof
    and the standing CI drift guard."""
    gmail = _REPO_ROOT / "packages" / "carpenter-gmail"
    if not (gmail / "compose.yaml").is_file():
        pytest.skip("carpenter-gmail compose.yaml not present")
    result = verify(gmail, repo_root=_REPO_ROOT)
    assert result.ok, result.summary()
    # The layer contributes the full move-list (31 files).
    assert result.checked == 31, result.summary()


def test_repo_root_autodetection_finds_layers_dir() -> None:
    """Without an explicit repo_root, the tool locates the layers dir
    by walking up from the leaf."""
    gmail = _REPO_ROOT / "packages" / "carpenter-gmail"
    if not (gmail / "compose.yaml").is_file():
        pytest.skip("carpenter-gmail compose.yaml not present")
    root = compose_mod._repo_root_from_leaf(gmail.resolve())
    assert (root / "layers" / "carpenter-email-core").is_dir()
