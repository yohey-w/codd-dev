"""Tests for the import-coherence CONTRACT projection into the implement prompt.

The verify-stage import-coherence gate (``codd.import_coherence.
check_import_coherence``) rejects a build whose source lives outside the
harness-owned package root (``source_outside_package``) or whose tests import a
source module by bare basename (``bare_basename_import``). Before this projection
the implement prompt never told the model WHICH package root the harness owns or
WHICH import style the gate requires, so the model freelanced: the ExprCalcPy
Python greenfield laid source in ``src/exprcalc/`` while the harness owns
``src/exprcalcpy/``, and unit tests bare-imported ``evaluator``/``parser`` — both
rejected by the gate with no way to prevent it up front.

These tests pin the projection: the SAME :class:`~codd.project_types.LayoutProfile`
the gate reads is rendered into the prompt as an explicit, DATA-DRIVEN contract,
and it is a strict no-op for a path-relative stack (no named package).
"""

from __future__ import annotations

from pathlib import Path

from codd.import_coherence import render_import_coherence_contract
from codd.implementer import DesignContext, ImplementSpec, _build_implementation_prompt
from codd.project_types import LayoutProfile


def _python_like_profile(package_name: str = "exprcalcpy") -> LayoutProfile:
    # defaults: requires_package_init=True, test_import_policy="package_absolute".
    return LayoutProfile(
        language="python",
        package_name=package_name,
        source_root="src",
        package_root=f"src/{package_name}",
        test_root="tests",
    )


def _relative_like_profile() -> LayoutProfile:
    # A TypeScript-style path-relative stack: no named package, relative imports.
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


# ---------------------------------------------------------------------------
# render_import_coherence_contract — the language-free projection seam
# ---------------------------------------------------------------------------


def test_render_none_returns_empty():
    assert render_import_coherence_contract(None) == ""


def test_render_relative_stack_returns_empty():
    # A path-relative stack has no named-package contract; BOTH package checks are
    # a no-op for it, so nothing is projected (no non-opt-in default).
    assert render_import_coherence_contract(_relative_like_profile()) == ""


def test_render_package_absolute_enumerates_both_rules():
    block = render_import_coherence_contract(_python_like_profile())
    assert block  # non-empty
    # Rule 1: source under the harness-owned package root, by its ACTUAL value.
    assert "src/exprcalcpy" in block
    assert "source_outside_package" in block
    # Rule 2: package-absolute import idiom, derived from package_name.
    assert "from exprcalcpy.<module> import" in block
    assert "bare_basename_import" in block
    # Release-blocking framing so the model treats it as a gate, not a hint.
    assert "release-blocking" in block.lower()


def test_render_is_data_driven_not_hardcoded():
    # A DIFFERENT project name must flow through verbatim — no hardcoded package.
    block = render_import_coherence_contract(_python_like_profile("calclib"))
    assert "src/calclib" in block
    assert "from calclib.<module> import" in block
    assert "exprcalcpy" not in block


def test_render_is_language_free():
    # The block must not name the language — dispatch is on profile DATA, not a
    # language literal (Contract Kernel principle). "PYTHONPATH" would leak the
    # language too, so the search-path rationale stays neutral.
    block = render_import_coherence_contract(_python_like_profile())
    assert "python" not in block.lower()


def test_render_rules_gate_independently():
    # requires_package_init WITHOUT package_absolute → only the package-root rule.
    only_init = LayoutProfile(
        language="x",
        package_name="pkg",
        source_root="src",
        package_root="src/pkg",
        test_root="tests",
        test_import_policy="flat",
        requires_package_init=True,
    )
    block = render_import_coherence_contract(only_init)
    assert "source_outside_package" in block
    assert "bare_basename_import" not in block

    # package_absolute WITHOUT requires_package_init → only the import rule.
    only_abs = LayoutProfile(
        language="x",
        package_name="pkg",
        source_root="src",
        package_root="src",
        test_root="tests",
        test_import_policy="package_absolute",
        requires_package_init=False,
    )
    block2 = render_import_coherence_contract(only_abs)
    assert "bare_basename_import" in block2
    assert "source_outside_package" not in block2


# ---------------------------------------------------------------------------
# Implement-prompt injection — resolved from the config, data-driven, no-op-safe
# ---------------------------------------------------------------------------


def _make_python_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project:\n  name: ExprCalcPy\n  language: python\n",
        encoding="utf-8",
    )
    return project


def test_implement_prompt_injects_layout_contract_for_source_task(tmp_path):
    project = _make_python_project(tmp_path)
    prompt = _build_implementation_prompt(
        config={"project": {"name": "ExprCalcPy", "language": "python"}},
        design_context=DesignContext(
            node_id="design:tokenizer",
            path=Path("docs/detailed_design/tokenizer_design.md"),
            content="# Tokenizer\n",
        ),
        spec=ImplementSpec("design:tokenizer", ["src"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )
    assert "import-coherence CONTRACT" in prompt
    # package name derived from the project name "ExprCalcPy" → exprcalcpy.
    assert "src/exprcalcpy" in prompt
    assert "from exprcalcpy.<module> import" in prompt


def test_implement_prompt_injects_layout_contract_for_test_task(tmp_path):
    # The contract applies to test tasks too (the bare-basename-import rule is
    # what unit tests violate); it renders regardless of source/test scope.
    project = _make_python_project(tmp_path)
    prompt = _build_implementation_prompt(
        config={
            "project": {"name": "ExprCalcPy", "language": "python"},
            "scan": {"test_dirs": ["tests/"]},
        },
        design_context=DesignContext(
            node_id="test:tokenizer",
            path=Path("docs/test/test_strategy.md"),
            content="# Test Strategy\n",
        ),
        spec=ImplementSpec("test:tokenizer", ["tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )
    assert "import-coherence CONTRACT" in prompt
    assert "from exprcalcpy.<module> import" in prompt


def test_implement_prompt_omits_layout_contract_without_project_root():
    # A DI/standalone caller with no project_root cannot resolve the profile; it
    # degrades to the prior prompt, never crashes.
    prompt = _build_implementation_prompt(
        config={"project": {"name": "ExprCalcPy", "language": "python"}},
        design_context=DesignContext(
            node_id="design:tokenizer",
            path=Path("docs/detailed_design/tokenizer_design.md"),
            content="# Tokenizer\n",
        ),
        spec=ImplementSpec("design:tokenizer", ["src"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )
    assert "import-coherence CONTRACT" not in prompt
