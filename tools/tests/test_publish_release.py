"""Tests for the GitHub-Releases package-archive publisher.

These mirror carpenter-core's ``tests/packages/test_archive_cache.py``
determinism + round-trip assertions, because the WHOLE point of this tool is
that its archives expand to a tree that hashes identically to what
carpenter-core's ``compute_package_hash`` measured at install time.  Concretely
we assert:

* identical tree -> byte-identical archive (reproducible builds);
* the expanded archive recomputes to the same root hash the builder returned
  (the verification carpenter-core's ``load_pristine_tree`` performs);
* the builder's hash matches an *independent* SHA-256 walk using the exact
  contract (sorted POSIX rel paths, length-prefixed framing, ignore rules);
* naming convention (tag/asset) is the one the consumer fetcher expects;
* ignore rules drop ``__pycache__``/``.pyc``/etc. so cruft never changes the
  archive;
* the real repo packages build and round-trip (expand -> rehash == build hash).

If carpenter-core ever changes ``archive_tree``/``compute_package_hash``, the
mirrored copies in ``tools/publish_release.py`` (and these tests) must change
in lockstep — that is the contract these tests guard.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

import pytest

# Make ``tools`` importable when tests run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.publish_release import (  # noqa: E402
    PublishError,
    archive_tree,
    asset_name,
    build_archive,
    compute_package_hash,
    read_manifest_version,
    release_tag,
)


# ── helpers ──────────────────────────────────────────────────────────


def _make_tree(root: Path) -> dict[str, bytes]:
    """Materialize a small synthetic package tree; return path->bytes.

    Mirrors carpenter-core's test fixture so behaviour is directly
    comparable.
    """
    contents: dict[str, bytes] = {
        "manifest.yaml": b"name: demo\nversion: 1.0.0\n",
        "tools.py": b"def hello():\n    return 'hi'\n",
        "data/blob.bin": bytes(range(256)),
        "nested/deep/note.txt": "unicode: \u2603\n".encode("utf-8"),
    }
    for rel, data in contents.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return contents


def _expand(archive: Path) -> dict[str, bytes]:
    """Expand a .tar.gz into a path->bytes mapping (test-local, trusted)."""
    out: dict[str, bytes] = {}
    with tarfile.open(str(archive), mode="r:*") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            f = tar.extractfile(member)
            assert f is not None
            out[member.name] = f.read()
    return out


def _independent_hash(root: Path) -> str:
    """A from-scratch reimplementation of the hashing contract.

    Deliberately does NOT import the production code, so a regression in
    ``compute_package_hash`` cannot make this test pass vacuously.  The
    contract: walk files (skipping ignored names/suffixes), hash each file's
    bytes, then accumulate ``(len(rel), rel, len(digest), digest)`` per file —
    in **carpenter-core's iteration order**, which is ``os.walk`` with each
    directory's children sorted (NOT a single global sort of full paths).
    """
    import os

    ignored_names = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".DS_Store",
        ".git",
        ".gitignore",
    }
    ignored_suffixes = (".pyc", ".pyo", ".swp", "~")
    root_resolved = root.resolve()
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root_resolved, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in ignored_names)
        for fn in sorted(filenames):
            if fn in ignored_names:
                continue
            if any(fn.endswith(s) for s in ignored_suffixes):
                continue
            files.append(Path(dirpath) / fn)
    acc = hashlib.sha256()
    for path in files:
        rel = path.relative_to(root_resolved).as_posix()
        fh = hashlib.sha256()
        fh.update(path.read_bytes())
        rel_bytes = rel.encode("utf-8")
        digest = fh.digest()
        acc.update(len(rel_bytes).to_bytes(4, "big"))
        acc.update(rel_bytes)
        acc.update(len(digest).to_bytes(4, "big"))
        acc.update(digest)
    return acc.hexdigest()


# ── determinism ──────────────────────────────────────────────────────


def test_archive_tree_is_byte_deterministic(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(src)
    a = tmp_path / "a.tar.gz"
    b = tmp_path / "b.tar.gz"
    archive_tree(src, a)
    archive_tree(src, b)
    assert a.read_bytes() == b.read_bytes()


def test_archive_gzip_header_has_no_mtime(tmp_path):
    """gzip mtime must be 0 so the compressed bytes are reproducible."""
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(src)
    out = tmp_path / "x.tar.gz"
    archive_tree(src, out)
    raw = out.read_bytes()
    # gzip header bytes 4..8 are the mtime (little-endian); must be zero.
    assert raw[:2] == b"\x1f\x8b"
    assert raw[4:8] == b"\x00\x00\x00\x00"


def test_members_normalized_and_sorted(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(src)
    out = tmp_path / "x.tar.gz"
    archive_tree(src, out)
    with tarfile.open(str(out), mode="r:*") as tar:
        members = tar.getmembers()
    names = [m.name for m in members]
    assert names == sorted(names)
    for m in members:
        assert m.mtime == 0
        assert m.uid == 0 and m.gid == 0
        assert m.uname == "" and m.gname == ""
        assert m.mode == 0o644
        assert m.isfile()
        assert not m.name.startswith("/")
        assert not m.name.startswith("./")


# ── hash equivalence (the consumer-verification contract) ────────────


def test_expanded_archive_hashes_equal_source(tmp_path):
    """The crux: expanding the archive recomputes the source root hash.

    This is exactly what carpenter-core's ``load_pristine_tree`` does before
    trusting a fetched archive.
    """
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(src)
    expected = compute_package_hash(src)

    out = tmp_path / "x.tar.gz"
    returned = archive_tree(src, out)
    assert returned == expected

    # Re-materialize the expanded tree and rehash it.
    expanded = tmp_path / "expanded"
    expanded.mkdir()
    for rel, data in _expand(out).items():
        p = expanded / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    assert compute_package_hash(expanded) == expected


def test_hash_matches_independent_implementation(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(src)
    assert compute_package_hash(src) == _independent_hash(src)


def test_round_trip_contents_preserved(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    original = _make_tree(src)
    out = tmp_path / "x.tar.gz"
    archive_tree(src, out)
    assert _expand(out) == original


# ── ignore rules ─────────────────────────────────────────────────────


def test_ignored_cruft_does_not_change_archive(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_tree(src)
    clean = tmp_path / "clean.tar.gz"
    h_clean = archive_tree(src, clean)

    # Add cruft that the ignore rules must drop.
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"junk")
    (src / "tools.pyc").write_bytes(b"junk")
    (src / ".DS_Store").write_bytes(b"junk")
    (src / "note.txt~").write_bytes(b"junk")
    (src / ".git").mkdir()
    (src / ".git" / "config").write_bytes(b"[core]\n")

    dirty = tmp_path / "dirty.tar.gz"
    h_dirty = archive_tree(src, dirty)

    assert h_clean == h_dirty
    assert clean.read_bytes() == dirty.read_bytes()


# ── naming convention (must match the consumer fetcher) ──────────────


def test_naming_convention():
    assert release_tag("carpenter-gmail", "0.7.0") == "carpenter-gmail-v0.7.0"
    assert asset_name("carpenter-gmail", "0.7.0") == "carpenter-gmail-0.7.0.tar.gz"


# ── manifest reading ─────────────────────────────────────────────────


def test_read_manifest_version(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "manifest.yaml").write_text('name: demo\nversion: "1.2.3"\n')
    assert read_manifest_version(pkg) == "1.2.3"


def test_read_manifest_version_missing_raises(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "manifest.yaml").write_text("name: demo\n")
    with pytest.raises(PublishError):
        read_manifest_version(pkg)


def test_archive_tree_rejects_non_dir(tmp_path):
    with pytest.raises(PublishError):
        archive_tree(tmp_path / "nope", tmp_path / "out.tar.gz")


# ── real-repo packages build + round-trip ────────────────────────────


@pytest.mark.parametrize(
    "name", ["hello", "carpenter-gmail", "carpenter-imap-email"]
)
def test_real_package_builds_and_round_trips(name, tmp_path):
    pkg_dir = _REPO_ROOT / "packages" / name
    if not pkg_dir.is_dir():
        pytest.skip(f"package {name} not present")
    out = tmp_path / "a.tar.gz"
    version, root_hash = build_archive(name, out)
    assert version  # non-empty
    assert root_hash == compute_package_hash(pkg_dir)

    # Expand and confirm it rehashes to the same root hash.
    expanded = tmp_path / "expanded"
    expanded.mkdir()
    for rel, data in _expand(out).items():
        p = expanded / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    assert compute_package_hash(expanded) == root_hash


def test_build_is_byte_deterministic_for_real_package(tmp_path):
    pkg_dir = _REPO_ROOT / "packages" / "hello"
    if not pkg_dir.is_dir():
        pytest.skip("hello package not present")
    a = tmp_path / "a.tar.gz"
    b = tmp_path / "b.tar.gz"
    build_archive("hello", a)
    build_archive("hello", b)
    assert a.read_bytes() == b.read_bytes()
