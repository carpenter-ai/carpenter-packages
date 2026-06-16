#!/usr/bin/env python3
"""Publish a capability-package version's installable tree as a GitHub Release.

This is the **publishing side** of Carpenter's package-upgrade reconcile
system (see ``~/notes/carpenter-package-archive-publishing-plan.md`` and the
companion reconciliation plan).  The reconcile engine needs, for any installed
package version, the *pristine* tree that version shipped, so it can three-way
diff OLD vs NEW vs the user's CURRENT on-disk copy.  ``shipped_old`` comes from
a local archive cache with a **remote fetch fallback** when the cache is
evicted.  This tool produces the artifact that fallback fetches: one GitHub
Release per package version, carrying the installable tree as a single,
deterministically-built ``.tar.gz``.

Installable tree (what gets archived)
-------------------------------------
carpenter-core's ``install_package`` copies a package's **source dir as-is**
and the registry scans ``packages/<name>/`` verbatim — core has no knowledge
of ``compose.yaml``.  Per ``layers/README.md``, the layer model uses *committed
composed copies*: each leaf KEEPS a complete, installable copy of every layer
file in its own directory (no cutover/duplicate-removal is adopted).  So the
installable tree the platform loads and ``compute_package_hash`` measures is
the **on-disk ``packages/<name>/`` directory** (including ``compose.yaml``), and
that is exactly what we archive.  ``tools.compose`` is the layer *edit-source*
and drift guard, NOT a runtime composition step; this tool runs
``tools.compose.verify`` as a publish-time guard (refusing to ship a leaf that
has drifted from its layer) but archives the on-disk dir directly.

Determinism contract (MUST stay byte-identical to carpenter-core)
-----------------------------------------------------------------
The downloaded archive is verified by carpenter-core's
``archive_cache.load_pristine_tree`` by **re-expanding it and recomputing**
``installer.compute_package_hash`` over the expanded tree, then comparing
against the trusted root hash recorded in ``installed_packages.hash``.  So the
*expanded tree* — not the archive bytes — is what must hash identically.  In
practice we go further and reproduce carpenter-core's
``archive_cache.archive_tree`` byte-for-byte, so the published asset is also
content-addressable and bit-reproducible.  The contract, mirrored exactly in
:func:`archive_tree` below, is:

* **Member set / ordering:** every regular file under the installable tree, with
  the same ignore rules carpenter-core's ``installer._iter_files`` applies
  (skip ``__pycache__``, ``.pytest_cache``, ``.mypy_cache``, ``.ruff_cache``,
  ``.DS_Store``, ``.git``, ``.gitignore`` directories/files; skip ``.pyc``,
  ``.pyo``, ``.swp`` and ``~`` suffixes).  Members are written in sorted
  POSIX-relative-path order.
* **Member names:** POSIX relative paths (forward slashes), no leading ``./``.
* **Normalized metadata:** every member has ``mtime=0``, ``uid=0``, ``gid=0``,
  empty ``uname``/``gname``, ``mode=0o644``, ``type=REGTYPE``.
* **Gzip:** ``gzip.GzipFile(filename="", mtime=0)`` so the gzip header carries
  no timestamp/name — the compressed bytes are reproducible.
* **No directory members:** only regular files are emitted (carpenter-core's
  hash walks files only; directories are implied by member paths on extract).

If carpenter-core's ``archive_tree``/``compute_package_hash`` ever change, the
copies here (and ``_IGNORED_NAMES``/``_IGNORED_SUFFIXES``) must change in
lockstep.  The test suite (``tools/tests/test_publish_release.py``) mirrors
carpenter-core's ``test_archive_cache`` round-trip + determinism assertions to
guard this.

Naming / lookup convention (publisher and fetcher MUST agree)
-------------------------------------------------------------
* Release **tag**:   ``<name>-v<version>``   (e.g. ``carpenter-gmail-v0.7.0``)
* Release **asset**: ``<name>-<version>.tar.gz``
* Releases are **per package** so versions are independent.  The tool is
  idempotent and NEVER deletes old releases or assets (historical versions
  must remain fetchable to reconstruct ``shipped_old`` for any installed
  version).  Re-running for an existing tag updates the asset in place.

Usage
-----
    # Build + publish (needs GITHUB_TOKEN with contents:write on the repo):
    python -m tools.publish_release carpenter-gmail

    # Build the archive and print the tag/asset/hash WITHOUT publishing:
    python -m tools.publish_release carpenter-gmail --dry-run

    # Write the archive somewhere for inspection:
    python -m tools.publish_release carpenter-gmail --dry-run --out /tmp/x.tar.gz
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency hint
    raise SystemExit(
        "PyYAML is required for publish_release: pip install pyyaml"
    ) from exc

# Reuse the canonical composition logic.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.compose import ComposeError  # noqa: E402

# ── carpenter-core hashing contract (kept in lockstep) ───────────────
# Mirror of carpenter-core ``installer._iter_files`` ignore rules.  These
# MUST match carpenter-core or published archives will hash differently from
# what ``compute_package_hash`` measured at install time.
_IGNORED_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".DS_Store",
        ".git",
        ".gitignore",
    }
)
_IGNORED_SUFFIXES = (".pyc", ".pyo", ".swp", "~")

GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "carpenter-ai/carpenter-packages"


class PublishError(RuntimeError):
    """Raised on any publish failure (bad manifest, API error, etc.)."""


# ── deterministic tree iteration + hashing (mirror of carpenter-core) ─


def _iter_files(root: Path) -> list[Path]:
    """Every regular file under ``root``, sorted — mirror of carpenter-core.

    Replicates ``carpenter.packages.installer._iter_files``: symlinks are not
    followed; ignored dir/file names and suffixes are skipped; results are
    sorted for determinism.
    """
    out: list[Path] = []
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root_resolved, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_NAMES)
        for fn in sorted(filenames):
            if fn in _IGNORED_NAMES:
                continue
            if any(fn.endswith(suf) for suf in _IGNORED_SUFFIXES):
                continue
            out.append(Path(dirpath) / fn)
    return out


def compute_package_hash(package_dir: Path) -> str:
    """Deterministic SHA-256 over a package dir — mirror of carpenter-core.

    Replicates ``carpenter.packages.installer.compute_package_hash`` exactly so
    a published asset, once expanded, recomputes to the same root hash the
    installer recorded.  See module docstring for the full contract.
    """
    package_dir = Path(package_dir).resolve()
    if not package_dir.is_dir():
        raise PublishError(
            f"compute_package_hash: {package_dir} is not a directory",
        )
    accumulator = hashlib.sha256()
    for path in _iter_files(package_dir):
        rel = path.relative_to(package_dir).as_posix()
        file_hash = hashlib.sha256()
        if path.is_symlink():
            target = os.readlink(path)
            file_hash.update(b"link:")
            file_hash.update(target.encode("utf-8", errors="surrogateescape"))
        else:
            with open(path, "rb") as fp:
                while True:
                    chunk = fp.read(65536)
                    if not chunk:
                        break
                    file_hash.update(chunk)
        rel_bytes = rel.encode("utf-8")
        digest = file_hash.digest()
        accumulator.update(len(rel_bytes).to_bytes(4, "big"))
        accumulator.update(rel_bytes)
        accumulator.update(len(digest).to_bytes(4, "big"))
        accumulator.update(digest)
    return accumulator.hexdigest()


def archive_tree(source_dir: Path, out_path: Path) -> str:
    """Create a deterministic ``.tar.gz`` of ``source_dir`` — mirror of
    carpenter-core ``archive_cache.archive_tree``.

    Returns the tree's root hash (``compute_package_hash(source_dir)``).  The
    produced bytes are byte-identical to what carpenter-core would produce for
    the same tree.  See module docstring for the determinism contract.
    """
    source_dir = Path(source_dir).resolve()
    if not source_dir.is_dir():
        raise PublishError(f"archive_tree: {source_dir} is not a directory")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    root_hash = compute_package_hash(source_dir)

    files = _iter_files(source_dir)
    rel_paths = sorted(p.relative_to(source_dir).as_posix() for p in files)

    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=out_path.name + ".", dir=str(out_path.parent),
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        with open(tmp_path, "wb") as raw:
            with gzip.GzipFile(
                filename="", fileobj=raw, mode="wb", mtime=0,
            ) as gz:
                with tarfile.open(fileobj=gz, mode="w") as tar:
                    for rel in rel_paths:
                        abs_path = source_dir / rel
                        info = tarfile.TarInfo(name=rel)
                        data = abs_path.read_bytes()
                        info.size = len(data)
                        info.mtime = 0
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mode = 0o644
                        info.type = tarfile.REGTYPE
                        tar.addfile(info, io.BytesIO(data))
        os.replace(str(tmp_path), str(out_path))
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    return root_hash


# ── manifest / naming ────────────────────────────────────────────────


def read_manifest_version(package_dir: Path) -> str:
    """Read the ``version`` field from a package's ``manifest.yaml``."""
    manifest = Path(package_dir) / "manifest.yaml"
    if not manifest.is_file():
        raise PublishError(f"no manifest.yaml at {manifest}")
    with manifest.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise PublishError(f"{manifest}: top-level YAML must be a mapping")
    version = data.get("version")
    if not version:
        raise PublishError(f"{manifest}: missing 'version'")
    return str(version)


def release_tag(name: str, version: str) -> str:
    """Release tag for a package version: ``<name>-v<version>``."""
    return f"{name}-v{version}"


def asset_name(name: str, version: str) -> str:
    """Release asset filename: ``<name>-<version>.tar.gz``."""
    return f"{name}-{version}.tar.gz"


def package_dir_for(name: str, *, repo_root: Path | None = None) -> Path:
    """Resolve ``packages/<name>/`` under the repo root."""
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    pkg = root / "packages" / name
    if not pkg.is_dir():
        raise PublishError(f"no package directory at {pkg}")
    return pkg


# ── build (drift-guard + archive) ────────────────────────────────────


def check_compose_drift(pkg_dir: Path, *, repo_root: Path | None = None) -> None:
    """Drift guard: if the leaf declares layers, assert no layer drift.

    Per ``layers/README.md``, the integration model is *committed composed
    copies* — each leaf KEEPS a complete, installable copy of every layer file
    in its own directory, and ``install_package`` copies that source dir
    **as-is** (the registry scans ``packages/<name>/`` verbatim).  The layer is
    only an edit-source; ``tools.compose.verify`` is the standing drift guard
    that every layer-contributed file is byte-identical to the leaf's committed
    copy.

    We therefore archive the **on-disk package directory** as the installable
    tree (see :func:`resolve_installable_tree`), but first run ``verify`` as a
    publish-time safety check: if a leaf's committed copy has drifted from its
    layer, we refuse to publish so a divergent tree never ships.
    """
    from tools.compose import verify  # local import to keep top-level light

    compose_yaml = pkg_dir / "compose.yaml"
    if not compose_yaml.is_file():
        return  # no layers declared (e.g. ``hello``) — nothing to check
    result = verify(pkg_dir, repo_root=repo_root)
    if not result.ok:
        raise PublishError(
            f"compose drift detected for {pkg_dir.name}; refusing to "
            f"publish a divergent tree:\n{result.summary()}",
        )


def resolve_installable_tree(
    pkg_dir: Path,
    *,
    repo_root: Path | None = None,
) -> tuple[Path, bool]:
    """Resolve the installable tree for a package.

    Returns ``(tree_dir, is_temp)``.  When ``is_temp`` is ``True`` the caller
    owns cleanup of ``tree_dir``.

    Per ``layers/README.md`` and carpenter-core's ``install_package`` (which
    copies the source dir **as-is** — the registry scans ``packages/<name>/``
    verbatim, and core has no knowledge of ``compose.yaml``), the installable
    tree the platform loads and ``compute_package_hash`` measures **is the
    on-disk package directory**, including any committed layer-file copies and
    the ``compose.yaml`` build descriptor.  So we archive ``pkg_dir`` directly.

    We do NOT run ``tools.compose.compose`` to overlay leaves on layers: that
    is a (deferred / not-adopted) post-cutover shape that would drop both the
    leaf's committed copies and ``compose.yaml``, producing a tree that does
    NOT match what the installer hashes today.  Instead :func:`build_archive`
    calls :func:`check_compose_drift` (``compose verify``) as a publish-time
    guard so the committed copies are proven faithful before shipping.

    The archived bytes are exactly what the installer hashes, so the published
    asset verifies against ``installed_packages.hash``.
    """
    return pkg_dir, False


def build_archive(
    name: str,
    out_path: Path,
    *,
    repo_root: Path | None = None,
) -> tuple[str, str]:
    """Resolve the installable tree for ``packages/<name>`` and archive it.

    Returns ``(version, root_hash)``.  Runs the compose drift guard first (for
    packages that declare layers), then archives the on-disk installable tree
    deterministically.  See :func:`resolve_installable_tree`.
    """
    import shutil

    pkg_dir = package_dir_for(name, repo_root=repo_root)
    version = read_manifest_version(pkg_dir)
    check_compose_drift(pkg_dir, repo_root=repo_root)
    tree, is_temp = resolve_installable_tree(pkg_dir, repo_root=repo_root)
    try:
        root_hash = archive_tree(tree, out_path)
    finally:
        if is_temp:
            shutil.rmtree(tree, ignore_errors=True)
    return version, root_hash


# ── GitHub API helpers ───────────────────────────────────────────────


def _api_request(
    method: str,
    url: str,
    token: str,
    *,
    data: bytes | None = None,
    content_type: str = "application/json",
    accept: str = "application/vnd.github+json",
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 - github api
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _get_release_by_tag(repo: str, tag: str, token: str) -> dict | None:
    status, body = _api_request(
        "GET", f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", token,
    )
    if status == 200:
        return json.loads(body)
    if status == 404:
        return None
    raise PublishError(
        f"GET release by tag {tag} failed ({status}): {body.decode(errors='replace')}",
    )


def _create_release(repo: str, tag: str, name: str, token: str) -> dict:
    payload = json.dumps(
        {
            "tag_name": tag,
            "name": name,
            "body": (
                f"Automated package-archive release for `{tag}`.\n\n"
                "Asset is the deterministically-composed installable tree, "
                "byte-reproducible per carpenter-core's `archive_cache."
                "archive_tree`. The reconcile fetcher downloads it and "
                "verifies it against the recorded install root hash."
            ),
            "draft": False,
            "prerelease": False,
        }
    ).encode("utf-8")
    status, body = _api_request(
        "POST", f"{GITHUB_API}/repos/{repo}/releases", token, data=payload,
    )
    if status not in (200, 201):
        raise PublishError(
            f"create release {tag} failed ({status}): {body.decode(errors='replace')}",
        )
    return json.loads(body)


def _find_asset(release: dict, asset: str) -> dict | None:
    for a in release.get("assets", []):
        if a.get("name") == asset:
            return a
    return None


def _delete_asset(repo: str, asset_id: int, token: str) -> None:
    status, body = _api_request(
        "DELETE", f"{GITHUB_API}/repos/{repo}/releases/assets/{asset_id}", token,
    )
    if status not in (200, 204):
        raise PublishError(
            f"delete existing asset {asset_id} failed ({status}): "
            f"{body.decode(errors='replace')}",
        )


def _upload_asset(
    upload_url: str, asset: str, archive_path: Path, token: str,
) -> dict:
    # ``upload_url`` is templated like ".../assets{?name,label}"; strip the
    # template and append our query.
    base = upload_url.split("{", 1)[0]
    url = f"{base}?name={asset}"
    data = Path(archive_path).read_bytes()
    status, body = _api_request(
        "POST", url, token, data=data, content_type="application/gzip",
    )
    if status not in (200, 201):
        raise PublishError(
            f"upload asset {asset} failed ({status}): {body.decode(errors='replace')}",
        )
    return json.loads(body)


def publish(
    name: str,
    *,
    repo: str = DEFAULT_REPO,
    token: str,
    repo_root: Path | None = None,
) -> dict:
    """Build + publish a package version's archive as a GitHub Release asset.

    Idempotent: creates the release for ``<name>-v<version>`` if absent, then
    uploads (replacing only the same-named asset) ``<name>-<version>.tar.gz``.
    NEVER deletes releases or other assets.

    Returns a small result dict (tag, asset, version, root_hash, release_url).
    """
    with tempfile.TemporaryDirectory(prefix="publish-release-") as tmp:
        out_path = Path(tmp) / "asset.tar.gz"
        version, root_hash = build_archive(name, out_path, repo_root=repo_root)
        tag = release_tag(name, version)
        asset = asset_name(name, version)

        release = _get_release_by_tag(repo, tag, token)
        if release is None:
            release = _create_release(repo, tag, tag, token)
            print(f"created release {tag}")
        else:
            print(f"release {tag} already exists; updating asset")

        existing = _find_asset(release, asset)
        if existing is not None:
            # Replace the same-named asset only (GitHub rejects duplicate
            # asset names). Old *releases* are never touched.
            _delete_asset(repo, existing["id"], token)
            print(f"removed stale asset {asset} for re-upload")

        uploaded = _upload_asset(
            release["upload_url"], asset, out_path, token,
        )
        print(f"uploaded {asset} ({uploaded.get('size')} bytes)")

    return {
        "tag": tag,
        "asset": asset,
        "version": version,
        "root_hash": root_hash,
        "release_url": release.get("html_url"),
    }


# ── CLI ──────────────────────────────────────────────────────────────


def _resolve_token(explicit: str | None) -> str:
    token = explicit or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise PublishError(
            "no GitHub token: pass --token or set GITHUB_TOKEN "
            "(needs contents:write on the repo)",
        )
    return token


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", help="package name (under packages/)")
    parser.add_argument(
        "--repo", default=DEFAULT_REPO, help=f"owner/repo (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--token", default=None, help="GitHub token (default: $GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the archive and print tag/asset/hash without publishing",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="(dry-run) write the built archive here for inspection",
    )
    args = parser.parse_args(argv)

    try:
        if args.dry_run:
            if args.out:
                out_path = Path(args.out)
                version, root_hash = build_archive(args.package, out_path)
                archive_loc = str(out_path)
            else:
                with tempfile.TemporaryDirectory(prefix="publish-dry-") as tmp:
                    out_path = Path(tmp) / "asset.tar.gz"
                    version, root_hash = build_archive(args.package, out_path)
                    archive_loc = "(temp, discarded)"
            tag = release_tag(args.package, version)
            asset = asset_name(args.package, version)
            print("DRY RUN — nothing published")
            print(f"  package:    {args.package}")
            print(f"  version:    {version}")
            print(f"  tag:        {tag}")
            print(f"  asset:      {asset}")
            print(f"  root_hash:  {root_hash}")
            print(f"  archive:    {archive_loc}")
            return 0

        token = _resolve_token(args.token)
        result = publish(args.package, repo=args.repo, token=token)
        print("PUBLISHED")
        for k, v in result.items():
            print(f"  {k}: {v}")
        return 0
    except (PublishError, ComposeError) as exc:
        print(f"publish error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
