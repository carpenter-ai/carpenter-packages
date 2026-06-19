"""Tests for the REVIEWER goal's inlined extract field schema.

The REVIEWER is an LLM step whose ONLY authoritative view of the extract
field list is its arc GOAL (the shipped ``reviewer.txt`` is NOT injected
into the LLM context anywhere — the platform's ``agent_roles`` profile
mechanism only contributes a generic system prompt).  Historically the
goal said "follow the static REVIEWER prompt shipped with this template"
and showed a placeholder ``submit_extract(fields={ ... })``, so the LLM
never saw the real field names and hallucinated its own schema
(``attachment_count``, ``classification``, ``flags`` …), which the JUDGE
then failed to decode.

These tests prove the goal now inlines the EXACT dataclass field list +
the closed ``category`` enum, derived from the dataclass itself so it
can never drift.

Run via ``~/bin/run-tests`` (NEVER bare pytest).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import types
from pathlib import Path

import pytest

LAYER = Path(__file__).resolve().parents[2] / "layers" / "carpenter-email-core"


def _load_layer():
    """Load the carpenter-email-core layer's data_models + arc_builders
    as a synthetic package so the relative ``from . import data_models``
    resolves."""
    try:
        import carpenter_tools.policy.types  # noqa: F401
    except ImportError:
        pytest.skip("carpenter_tools not importable in this environment")

    pkgname = "_email_core_reviewer_goal_test"
    if pkgname in sys.modules:
        return sys.modules[pkgname + ".arc_builders"], sys.modules[
            pkgname + ".data_models"
        ]
    pkg = types.ModuleType(pkgname)
    pkg.__path__ = [str(LAYER)]
    sys.modules[pkgname] = pkg

    def _load(mod):
        spec = importlib.util.spec_from_file_location(
            f"{pkgname}.{mod}", LAYER / f"{mod}.py"
        )
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
        return m

    dm = _load("data_models")
    ab = _load("arc_builders")
    return ab, dm


def test_triage_goal_inlines_exact_dataclass_fields():
    ab, dm = _load_layer()
    goal = ab._reviewer_goal("EmailTriageExtract")

    expected = [f.name for f in dataclasses.fields(dm.EmailTriageExtract)]
    # Every real field name appears in the goal text.
    for name in expected:
        assert name in goal, f"field {name!r} missing from REVIEWER goal"

    # The closed category enum is inlined verbatim.
    for cat in dm.EMAIL_TRIAGE_CATEGORIES:
        assert cat in goal, f"category {cat!r} missing from REVIEWER goal"

    # The submit_extract example uses the REAL keys, not a placeholder.
    assert "submit_extract(fields={" in goal
    assert "# ... the extract fields you computed" not in goal
    assert "'provider_message_id':" in goal
    assert "'category':" in goal
    assert "'schema_version':" in goal


def test_field_spec_derives_from_dataclass():
    ab, dm = _load_layer()
    names, cats = ab._extract_field_spec("EmailTriageExtract")
    assert names == [f.name for f in dataclasses.fields(dm.EmailTriageExtract)]
    assert cats == tuple(dm.EMAIL_TRIAGE_CATEGORIES)


def test_field_spec_no_category_enum_for_non_triage():
    ab, dm = _load_layer()
    # EmailSimpleTextExtract has no ``category`` field -> empty enum.
    names, cats = ab._extract_field_spec("EmailSimpleTextExtract")
    assert "category" not in names
    assert cats == ()
    # But its real fields still inline into the goal.
    goal = ab._reviewer_goal("EmailSimpleTextExtract")
    for name in names:
        assert name in goal


def test_unknown_kind_falls_back_gracefully():
    ab, _ = _load_layer()
    names, cats = ab._extract_field_spec("NoSuchKindXyz")
    assert names == []
    assert cats == ()
    # Goal still renders (falls back to the shipped-prompt language).
    goal = ab._reviewer_goal("NoSuchKindXyz")
    assert "submit_extract" in goal
