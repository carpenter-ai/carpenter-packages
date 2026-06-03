"""
Minimal acceptance-story framework for carpenter-gmail package-internal
stories.

These stories test the package in isolation (no running daemon, no
real Gmail traffic).  They share the same story-shape contract as the
platform-level acceptance stories that live in
``carpenter-linux/user_stories/``:

* A class with ``name`` and ``description`` attributes.
* A ``run(client, db) -> StoryResult`` method (``client`` and ``db``
  may both be ``None`` — the runner passes whatever it has, and these
  stories don't touch either).
* ``assert_that(condition, message, **diagnostics)`` raises
  ``AssertionFailure`` to signal a failed assertion.

The package-internal stories cover:

* Manifest declarations (version, declared tools, declared data
  models, declared trigger types, scopes).
* Chat-tool registry (decorator metadata, ``requires_user_confirm``
  flags, capability declarations).
* JUDGE handler golden cases (valid extracts approved, malformed
  inputs rejected with the documented reason text).
* Data-model dataclasses (``schema_version`` defaults, ``frozen=True``,
  expected fields).
* AST-shape lint of the inline EXECUTOR scripts.

What package-internal stories DELIBERATELY do not test:

* Anything that requires the carpenter daemon to be running.
* Anything that requires the work queue to dispatch an arc.
* Anything that round-trips through the chat agent / LLM.
* End-to-end trigger -> arc -> JUDGE -> chat-notify pipelines.

Those continue to live in carpenter-linux's user_stories/ as platform-
integration stories (s055-s058).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class AssertionFailure(Exception):
    """Raised by story assertions to signal a test failure."""
    message: str
    diagnostics: dict = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.message


@dataclass
class StoryResult:
    name: str
    passed: bool
    message: str = ""
    error: str = ""
    diagnostics: dict = field(default_factory=dict)
    duration_s: float = 0.0

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"[{status}] {self.name} ({self.duration_s:.1f}s)"


# ---------------------------------------------------------------------------
# Story base class (matches the platform contract)
# ---------------------------------------------------------------------------


class PackageStory:
    """Base class for a package-internal acceptance story.

    Subclasses must:

    * Set class attributes ``name`` and ``description``.
    * Implement ``run(client, db)`` returning a ``StoryResult``.

    ``client`` and ``db`` parameters mirror the platform contract so
    the same runner can drive both.  Package stories typically ignore
    both arguments.
    """

    name: str = "unnamed"
    description: str = ""
    timeout: int = 60  # Package-internal stories are quick.

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------

    def run(self, client: Any = None, db: Any = None) -> StoryResult:
        raise NotImplementedError

    def cleanup(self, client: Any = None, db: Any = None) -> None:
        """Called after run() completes (pass or fail).  Override if needed."""

    # ------------------------------------------------------------------
    # Assertion helpers (identical surface to the platform framework)
    # ------------------------------------------------------------------

    def assert_that(
        self, condition: bool, message: str, **diagnostics: Any,
    ) -> None:
        if not condition:
            raise AssertionFailure(message, diagnostics)

    def assert_equal(
        self, actual: Any, expected: Any, message: str = "",
    ) -> None:
        ctx = f"{message}: " if message else ""
        self.assert_that(
            actual == expected,
            f"{ctx}expected {expected!r}, got {actual!r}",
            actual=actual,
            expected=expected,
        )

    def assert_contains(
        self, container: Any, item: Any, message: str = "",
    ) -> None:
        ctx = f"{message}: " if message else ""
        self.assert_that(
            item in container,
            f"{ctx}{item!r} not in container",
            container=str(container)[:300],
            item=item,
        )

    def result(self, message: str = "") -> StoryResult:
        return StoryResult(name=self.name, passed=True, message=message)


# ---------------------------------------------------------------------------
# Package location helpers
# ---------------------------------------------------------------------------


def package_root() -> Path:
    """Return the carpenter-gmail package directory (parent of
    ``user_stories/``).
    """
    return Path(__file__).resolve().parent.parent


def ensure_carpenter_on_path() -> None:
    """Add the canonical carpenter source dirs to ``sys.path`` if they're
    not already importable.

    Most package stories only need ``carpenter.packages.manifest`` /
    ``carpenter.packages.handler_registry`` / ``carpenter.packages.loaders``
    to load the package's own artifacts.  We try the editable install
    first (``import carpenter``) and fall back to the canonical repo
    location if it's not pip-installed.
    """
    try:
        import carpenter  # noqa: F401
        return
    except ImportError:
        pass

    candidates = (
        os.environ.get("CARPENTER_CORE_REPO", ""),
        str(Path.home() / "repos" / "carpenter-core"),
    )
    for candidate in candidates:
        if not candidate:
            continue
        p = Path(candidate)
        if p.is_dir() and (p / "carpenter" / "__init__.py").is_file():
            sys.path.insert(0, str(p))
            return
    # If we couldn't find it, leave sys.path alone — the subsequent
    # ``import carpenter`` will raise ImportError; the caller can set
    # ``CARPENTER_CORE_REPO`` to point at a checkout or pip-install the
    # ``carpenter-ai`` package to resolve it.


def load_package_module(name: str):
    """Import a sibling module from this carpenter-gmail package via the
    same loader the platform uses at install time.

    ``name`` is the module path relative to the package root, e.g.
    ``"tools"`` or ``"handlers.triage_inbound"``.
    """
    ensure_carpenter_on_path()
    from carpenter.packages.loaders import _import_package_module

    return _import_package_module(
        "carpenter-gmail", name, package_root(),
    )
