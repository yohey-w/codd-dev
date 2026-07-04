"""Tests for the harness-owned LAYOUT-PLACEMENT contract projection.

The output-path fence drops any generated file that lands outside the task's
configured output roots. Before this projection the generation/implement prompt
never told the model WHERE the harness owns the test root (or the source root, or
the scaffold-owned config files), so the model freelanced a sibling test dir: the
JS greenfield wrote unit specs under ``test/`` while the harness owns ``tests/``,
the fence dropped the file, and the declared 'test' deliverable read as "not
produced" → a hard ``StageError`` at the kind gate.

These tests pin the projection: the SAME
:class:`~codd.project_types.LayoutProfile` the scaffold + fence read is rendered
into BOTH prompts as an explicit, DATA-DRIVEN contract that is language-free and a
strict no-op for a stack with no resolved layout (Go).
"""

from __future__ import annotations

from pathlib import Path

from codd.generator import WaveArtifact, _build_generation_prompt
from codd.implementer import DesignContext, ImplementSpec, _build_implementation_prompt
from codd.project_types import (
    LayoutProfile,
    render_layout_placement_contract,
    resolve_layout_profile,
)


def _js_like_profile() -> LayoutProfile:
    # A path-relative (TypeScript/JS-family) stack: no named package, source lands
    # directly under the source root, tests under ``tests``.
    return LayoutProfile(
        language="typescript",
        package_name="exprcalc",
        source_root="src",
        package_root="src",
        test_root="tests",
        test_import_policy="relative",
        requires_package_init=False,
        requires_test_init=False,
    )


def _python_like_profile() -> LayoutProfile:
    return LayoutProfile(
        language="python",
        package_name="exprcalcpy",
        source_root="src",
        package_root="src/exprcalcpy",
        test_root="tests",
    )


# ---------------------------------------------------------------------------
# render_layout_placement_contract — the language-free projection seam
# ---------------------------------------------------------------------------


def test_render_none_returns_empty():
    assert render_layout_placement_contract(None) == ""


def test_render_js_emits_test_root_and_source_root():
    block = render_layout_placement_contract(_js_like_profile())
    assert block
    # TEST ROOT rule — the owned test root, by its ACTUAL value.
    assert "`tests/`" in block
    # The freelanced sibling name the fix exists to prevent is named as forbidden.
    assert "`test/`" in block
    # SOURCE ROOT rule — emitted for a path-relative stack (no import contract).
    assert "`src/`" in block
    assert "SOURCE LOCATION" in block
    # Release-blocking framing so the model treats it as a gate, not a hint.
    assert "release-blocking" in block.lower()


def test_render_python_coexists_without_duplicating_source_root_rule():
    block = render_layout_placement_contract(_python_like_profile())
    assert block
    # The TEST ROOT rule still renders for a named-package stack.
    assert "TEST LOCATION" in block
    assert "`tests/`" in block
    # But NOT the SOURCE ROOT rule — the import contract (render_import_coherence_
    # contract rule 1) already states the source-under-package rule for Python;
    # re-emitting it here would duplicate it.
    assert "SOURCE LOCATION" not in block


def test_render_ts_legacy_profile_renders():
    # The real resolver returns a genuine LayoutProfile for the TS/node stack; the
    # contract must render from it (this is the seam a future TS emit contract
    # extends).
    profile = resolve_layout_profile(
        language="typescript",
        project_name="ExprCalc",
        source_dirs=["src"],
        test_dirs=["tests"],
    )
    assert profile is not None
    block = render_layout_placement_contract(profile)
    assert "`tests/`" in block
    assert "`src/`" in block


def test_render_brownfield_test_dir_uses_owned_root_verbatim():
    # A stack whose owned test root IS ``test`` must render ``test/`` — and must NOT
    # forbid its OWN root (no hardcoded ``tests`` literal anywhere).
    profile = LayoutProfile(
        language="typescript",
        package_name="app",
        source_root="src",
        package_root="src",
        test_root="test",
        test_import_policy="relative",
        requires_package_init=False,
        requires_test_init=False,
    )
    block = render_layout_placement_contract(profile)
    assert "`test/`" in block  # owned root rendered verbatim
    # The forbidden-siblings illustration must EXCLUDE the owned root.
    assert "do NOT invent a sibling test directory such as" in block
    forbidden = block.split("do NOT invent a sibling test directory such as", 1)[1]
    forbidden = forbidden.split(")", 1)[0]
    assert "`test/`" not in forbidden  # never told to avoid its own owned root


def test_render_is_language_free():
    # The block must not name the language — dispatch is on profile DATA, not a
    # language literal (Contract Kernel principle).
    for profile in (_js_like_profile(), _python_like_profile()):
        block = render_layout_placement_contract(profile).lower()
        assert "python" not in block
        assert "typescript" not in block
        assert "javascript" not in block


def test_render_is_data_driven_not_hardcoded():
    # A DIFFERENT test root must flow through verbatim — no hardcoded ``tests``.
    profile = LayoutProfile(
        language="x",
        package_name="app",
        source_root="lib",
        package_root="lib",
        test_root="spec",
        test_import_policy="relative",
        requires_package_init=False,
        requires_test_init=False,
    )
    block = render_layout_placement_contract(profile)
    assert "`spec/`" in block
    assert "`lib/`" in block


# ---------------------------------------------------------------------------
# Generate-prompt injection
# ---------------------------------------------------------------------------


def _design_artifact() -> WaveArtifact:
    return WaveArtifact(
        wave=1,
        node_id="design:tokenizer",
        output="docs/detailed_design/tokenizer_design.md",
        title="Tokenizer",
        depends_on=[],
        conventions=[],
    )


def test_generation_prompt_injects_placement_contract():
    prompt = _build_generation_prompt(
        _design_artifact(),
        [],
        [],
        project_language="typescript",
        layout_placement=render_layout_placement_contract(_js_like_profile()),
    )
    assert "Repository LAYOUT CONTRACT" in prompt
    assert "`tests/`" in prompt
    assert "`src/`" in prompt


def test_generation_prompt_omits_placement_when_language_unknown():
    # No project_language → the language-scoped block (which the placement lives in)
    # is not emitted; a legacy/unknown caller keeps the prior prompt.
    prompt = _build_generation_prompt(
        _design_artifact(),
        [],
        [],
        project_language=None,
        layout_placement=render_layout_placement_contract(_js_like_profile()),
    )
    assert "Repository LAYOUT CONTRACT" not in prompt


# ---------------------------------------------------------------------------
# Implement-prompt injection
# ---------------------------------------------------------------------------


def _make_js_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project:\n  name: ExprCalc\n  language: typescript\n",
        encoding="utf-8",
    )
    return project


def test_implement_prompt_injects_placement_contract(tmp_path):
    project = _make_js_project(tmp_path)
    prompt = _build_implementation_prompt(
        config={
            "project": {"name": "ExprCalc", "language": "typescript"},
            "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
        },
        design_context=DesignContext(
            node_id="test:errors",
            path=Path("docs/test/test_strategy.md"),
            content="# Test Strategy\n",
        ),
        spec=ImplementSpec("test:errors", ["src", "tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )
    assert "Repository LAYOUT CONTRACT" in prompt
    assert "`tests/`" in prompt


def test_implement_prompt_omits_placement_without_project_root():
    prompt = _build_implementation_prompt(
        config={"project": {"name": "ExprCalc", "language": "typescript"}},
        design_context=DesignContext(
            node_id="test:errors",
            path=Path("docs/test/test_strategy.md"),
            content="# Test Strategy\n",
        ),
        spec=ImplementSpec("test:errors", ["src", "tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )
    assert "Repository LAYOUT CONTRACT" not in prompt
