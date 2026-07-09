"""Regression coverage for ``tools._resolve_expected_account``.

The chat-tool gating helper resolves the operator's mailbox address for
the T1 envelope recipient-mismatch check.  The value lives in the
per-package ``.env`` at
``{base_dir}/config/packages/carpenter-imap-email/.env`` and MUST be
read via :func:`resolve_package_secret` — the same resolver the trigger
poller and reflection SMTP dispatch use.

Regression: a prior version read from ``config.CONFIG`` only, so every
gated tool (``pkg_imap_send_email``, ``pkg_imap_reply_email``, etc.)
failed closed with "expected_account is not configured" even when the
credentials were present on disk.  Ben's live 2026-07-09 session
"Send Hello World Email" hit exactly this: the send tool returned
``{"error": "expected_account is not configured..."}`` and the email
never went out.

We load ``tools.py`` with a synthetic package name so its relative
``from .arc_builders import ...`` line resolves.  We do NOT need the
real ``arc_builders`` — a stub with the imported names is enough,
since ``_resolve_expected_account`` doesn't touch any of them.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


_PKG_ROOT = (
    Path(__file__).resolve().parents[2]
    / "packages" / "carpenter-imap-email"
)
_PKG_MOD_NAME = "_cie_pkg_under_test"


@pytest.fixture()
def tools_module(monkeypatch):
    """Import ``tools.py`` as a submodule of a synthetic package.

    The package has just enough stubs to satisfy ``tools.py``'s
    ``from carpenter...`` and ``from .arc_builders import ...`` lines
    without dragging in the real platform.
    """
    # ── carpenter.* stubs ─────────────────────────────────────────────
    carpenter = types.ModuleType("carpenter")
    sys.modules["carpenter"] = carpenter

    config_mod = types.ModuleType("carpenter.config")
    config_mod.CONFIG = {}
    carpenter.config = config_mod
    sys.modules["carpenter.config"] = config_mod

    packages_mod = types.ModuleType("carpenter.packages")
    carpenter.packages = packages_mod
    sys.modules["carpenter.packages"] = packages_mod

    capabilities_mod = types.ModuleType("carpenter.packages.capabilities")

    def _default_resolver(pkg, key):
        return None

    capabilities_mod.resolve_package_secret = _default_resolver
    packages_mod.capabilities = capabilities_mod
    sys.modules["carpenter.packages.capabilities"] = capabilities_mod

    chat_tool_loader = types.ModuleType("carpenter.chat_tool_loader")

    def chat_tool(*_a, **_kw):
        def _wrap(fn):
            return fn
        return _wrap

    chat_tool_loader.chat_tool = chat_tool
    carpenter.chat_tool_loader = chat_tool_loader
    sys.modules["carpenter.chat_tool_loader"] = chat_tool_loader

    # ── synthetic package that holds tools + stub siblings ────────────
    pkg = types.ModuleType(_PKG_MOD_NAME)
    pkg.__path__ = [str(_PKG_ROOT)]
    sys.modules[_PKG_MOD_NAME] = pkg

    arc_builders_stub = types.ModuleType(f"{_PKG_MOD_NAME}.arc_builders")
    for name in (
        "EXTRACT_KIND_BY_TEMPLATE",
        "_WRITE_EXTRACT_KIND_BY_TEMPLATE",
        "_build_raw_message",
        "_create_read_arc_tree",
        "_create_triage_arc_tree",
        "_create_write_arc_tree",
    ):
        setattr(arc_builders_stub, name, object())
    sys.modules[f"{_PKG_MOD_NAME}.arc_builders"] = arc_builders_stub

    tools_path = _PKG_ROOT / "tools.py"
    spec = importlib.util.spec_from_file_location(
        f"{_PKG_MOD_NAME}.tools", tools_path,
        submodule_search_locations=None,
    )
    tools = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG_MOD_NAME}.tools"] = tools
    spec.loader.exec_module(tools)

    yield tools

    for key in list(sys.modules):
        if key.startswith(_PKG_MOD_NAME) or key.startswith("carpenter"):
            sys.modules.pop(key, None)


def test_reads_from_package_env_via_resolve_package_secret(
    monkeypatch, tools_module,
):
    """The IMAP username in per-package .env must be found."""
    from carpenter.packages import capabilities

    def fake_resolve(pkg, key):
        assert pkg == "carpenter-imap-email"
        if key == "EMAIL_IMAP_USERNAME":
            return "carpenter-ai@mailbox.org"
        return None

    monkeypatch.setattr(capabilities, "resolve_package_secret", fake_resolve)

    assert tools_module._resolve_expected_account() == "carpenter-ai@mailbox.org"


def test_falls_back_to_operator_email_in_main_config(
    monkeypatch, tools_module,
):
    """If the package .env is unset, the platform-wide operator_email wins.

    Also asserts normalisation: trim + lower-case for the envelope check.
    """
    from carpenter import config
    from carpenter.packages import capabilities

    monkeypatch.setattr(
        capabilities, "resolve_package_secret", lambda p, k: None,
    )
    monkeypatch.setitem(config.CONFIG, "operator_email", "OP@Example.COM")

    assert tools_module._resolve_expected_account() == "op@example.com"


def test_returns_empty_when_neither_set(monkeypatch, tools_module):
    """Fail-closed sentinel: empty string when nothing resolves."""
    from carpenter import config
    from carpenter.packages import capabilities

    monkeypatch.setattr(
        capabilities, "resolve_package_secret", lambda p, k: None,
    )
    monkeypatch.setitem(config.CONFIG, "operator_email", "")

    assert tools_module._resolve_expected_account() == ""
