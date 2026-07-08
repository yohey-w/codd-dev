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

import pytest

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


# ---------------------------------------------------------------------------
# Multi-root source placement (C++ src/ + include/) — the projection-class fix
# for the flat-headers-in-src/ greenfield failure. A stack may OWN more than one
# source root; the single package_root rule cannot describe that (and would tell
# the model that files outside its ONE root are dropped), so a public header
# authored under a second owned root reads as contract-non-compliant. cpp is the
# only profile whose rendering changes; every single-root stack renders BYTE-FOR-
# BYTE as before (the golden no-op proof below).
# ---------------------------------------------------------------------------


def _cpp_profile() -> LayoutProfile:
    profile = resolve_layout_profile(
        language="cpp",
        project_name="ExprCalc",
        source_dirs=["src", "include"],
        test_dirs=["tests"],
    )
    assert profile is not None
    return profile


def test_cpp_renders_both_source_roots_and_reference_form():
    # RED-before-green anchor: on pre-fix code cpp renders only the single-root
    # SOURCE LOCATION rule for `src/` — `include/` and the SOURCE REFERENCE FORM
    # rule are absent, so this test fails until (a)/(b)/(c) land.
    block = render_layout_placement_contract(_cpp_profile())
    assert block
    # BOTH owned source roots are named as owned roots (not just one).
    assert "`src/`" in block
    assert "`include/`" in block
    # A SINGLE multi-root SOURCE LOCATION rule (distinct wording from the single-
    # root rule, so we know we took the multi-root branch).
    assert "SOURCE LOCATION" in block
    assert "owns MORE than one source root" in block
    assert "put EVERY source module you author UNDER" not in block
    # Each set's OWN file globs are rendered verbatim (placement made concrete
    # without any language noun — the globs are profile DATA).
    assert "`src/**/*.cpp`" in block
    assert "`include/**/*.h`" in block
    # The SOURCE REFERENCE FORM rule for the reference_base (include) set: a file
    # under the base root is referenced by its path RELATIVE TO that root, never a
    # bare same-directory filename (the exact enabling mechanism of the bug).
    assert "SOURCE REFERENCE FORM" in block
    assert "`include/<dir>/<name>.h`" in block
    assert "`<dir>/<name>.h`" in block
    assert "never by a bare filename" in block
    # Vocabulary-neutral core: no language-specific reference verb / noun leaks in
    # (the extensions come from the profile's globs, which is DATA — not a noun).
    low = block.lower()
    assert "#include" not in low
    assert "header" not in low


def test_synthesize_cpp_populates_source_placements():
    # RED-before-green: `source_placements` does not exist on pre-fix LayoutProfile
    # (AttributeError), and once it does it must carry exactly the two declared
    # source sets with reference_base ONLY on the `include` set.
    from codd.project_types import SourcePlacementSpec

    placements = _cpp_profile().source_placements
    assert len(placements) == 2
    assert all(isinstance(p, SourcePlacementSpec) for p in placements)
    by_root = {p.root: p for p in placements}
    assert set(by_root) == {"src", "include"}
    # reference_base is True ONLY for include (first_party rule=include_path_prefix,
    # base=include) — data-driven, never a language name.
    assert by_root["src"].reference_base is False
    assert by_root["include"].reference_base is True
    # Globs carried verbatim from the declarative profile.
    assert by_root["src"].file_globs == ("src/**/*.cpp", "src/**/*.cc", "src/**/*.cxx")
    assert by_root["include"].file_globs == (
        "include/**/*.h",
        "include/**/*.hpp",
        "include/**/*.hh",
    )
    # No-op proof at the DATA layer: the other synthesized single-source-set stacks
    # get exactly ONE placement and never reference_base (so they can never take
    # the multi-root branch or emit a reference-form rule).
    for lang in ("java", "csharp", "javascript"):
        other = resolve_layout_profile(
            language=lang,
            project_name="ExprCalc",
            source_dirs=["src"],
            test_dirs=["tests"],
        )
        assert other is not None
        assert len(other.source_placements) == 1
        assert other.source_placements[0].reference_base is False


# The rendered output for every single-root stack, captured verbatim (the
# render-contract golden). v3.18.0 ownership carve-out: the named-package FACADE
# `src/exprcalc/__init__.py` is now SUT-authored, so it is subtracted from the
# python HARNESS-OWNED SCAFFOLD "do NOT author" list (the model must populate it);
# `__main__.py` / `pyproject.toml` / test `__init__.py` stay harness-owned. Every
# other stack is byte-identical (facade_output_paths() is empty for them).
_GOLDEN_LAYOUT_CONTRACT = {
    'python': (
        'Repository LAYOUT CONTRACT (release-blocking — the harness scaffold owns this topology and the output-path fence enforces it at implement; a file placed outside the owned roots is dropped, so a declared deliverable then reads as never produced and fails the build — get the placement right the first time):' '\n'
        '' '\n'
        "1. TEST LOCATION — the harness OWNS the test root `tests/`, and the verify runner discovers test files ONLY under `tests/`. Put EVERY test file you author — and every test-file path this document references — UNDER `tests/` (do NOT invent a sibling test directory such as `test/`, `spec/`, `specs/`). A test file placed outside `tests/` is dropped by the output-path fence, so its declared 'test' deliverable reads as never produced and fails the build." '\n'
        '2. HARNESS-OWNED SCAFFOLD — the dependency manifest, the lockfile, and the test-runner / toolchain config files are created by the harness scaffold, and the verify command is fixed. Do NOT author or declare a runner/tool config file among your outputs — these are already provided: `pyproject.toml`, `src/exprcalc/__main__.py`, `tests/__init__.py`. A config file you emit is dropped by the output-path fence (and never changes how verify runs).'
    ),
    'javascript': (
        'Repository LAYOUT CONTRACT (release-blocking — the harness scaffold owns this topology and the output-path fence enforces it at implement; a file placed outside the owned roots is dropped, so a declared deliverable then reads as never produced and fails the build — get the placement right the first time):' '\n'
        '' '\n'
        "1. TEST LOCATION — the harness OWNS the test root `tests/`, and the verify runner discovers test files ONLY under `tests/`. Put EVERY test file you author — and every test-file path this document references — UNDER `tests/` (do NOT invent a sibling test directory such as `test/`, `spec/`, `specs/`). A test file placed outside `tests/` is dropped by the output-path fence, so its declared 'test' deliverable reads as never produced and fails the build." '\n'
        '2. SOURCE LOCATION — put EVERY source module you author UNDER `src/`. A source file placed outside `src/` is dropped by the output-path fence.' '\n'
        '3. HARNESS-OWNED SCAFFOLD — the dependency manifest, the lockfile, and the test-runner / toolchain config files are created by the harness scaffold, and the verify command is fixed. Do NOT author or declare a runner/tool config file among your outputs — these are already provided: `package.json`, `package-lock.json`. A config file you emit is dropped by the output-path fence (and never changes how verify runs).'
    ),
    'typescript': (
        'Repository LAYOUT CONTRACT (release-blocking — the harness scaffold owns this topology and the output-path fence enforces it at implement; a file placed outside the owned roots is dropped, so a declared deliverable then reads as never produced and fails the build — get the placement right the first time):' '\n'
        '' '\n'
        "1. TEST LOCATION — the harness OWNS the test root `tests/`, and the verify runner discovers test files ONLY under `tests/`. Put EVERY test file you author — and every test-file path this document references — UNDER `tests/` (do NOT invent a sibling test directory such as `test/`, `spec/`, `specs/`). A test file placed outside `tests/` is dropped by the output-path fence, so its declared 'test' deliverable reads as never produced and fails the build." '\n'
        '2. SOURCE LOCATION — put EVERY source module you author UNDER `src/`. A source file placed outside `src/` is dropped by the output-path fence.' '\n'
        '3. HARNESS-OWNED SCAFFOLD — the dependency manifest, the lockfile, and the test-runner / toolchain config files are created by the harness scaffold, and the verify command is fixed. Do NOT author or declare a runner/tool config file among your outputs — these are already provided: `package.json`, `package-lock.json`, `tsconfig.json`, `vitest.config.ts`. A config file you emit is dropped by the output-path fence (and never changes how verify runs).'
    ),
    'java': (
        'Repository LAYOUT CONTRACT (release-blocking — the harness scaffold owns this topology and the output-path fence enforces it at implement; a file placed outside the owned roots is dropped, so a declared deliverable then reads as never produced and fails the build — get the placement right the first time):' '\n'
        '' '\n'
        "1. TEST LOCATION — the harness OWNS the test root `src/test/java/`, and the verify runner discovers test files ONLY under `src/test/java/`. Put EVERY test file you author — and every test-file path this document references — UNDER `src/test/java/` (do NOT invent a sibling test directory such as `test/`, `tests/`, `spec/`, `specs/`). A test file placed outside `src/test/java/` is dropped by the output-path fence, so its declared 'test' deliverable reads as never produced and fails the build." '\n'
        '2. SOURCE LOCATION — put EVERY source module you author UNDER `src/main/java/`. A source file placed outside `src/main/java/` is dropped by the output-path fence.' '\n'
        '3. HARNESS-OWNED SCAFFOLD — the dependency manifest, the lockfile, and the test-runner / toolchain config files are created by the harness scaffold, and the verify command is fixed. Do NOT author or declare a runner/tool config file among your outputs — these are already provided: `pom.xml`. A config file you emit is dropped by the output-path fence (and never changes how verify runs).'
    ),
    'csharp': (
        'Repository LAYOUT CONTRACT (release-blocking — the harness scaffold owns this topology and the output-path fence enforces it at implement; a file placed outside the owned roots is dropped, so a declared deliverable then reads as never produced and fails the build — get the placement right the first time):' '\n'
        '' '\n'
        "1. TEST LOCATION — the harness OWNS the test root `tests/`, and the verify runner discovers test files ONLY under `tests/`. Put EVERY test file you author — and every test-file path this document references — UNDER `tests/` (do NOT invent a sibling test directory such as `test/`, `spec/`, `specs/`). A test file placed outside `tests/` is dropped by the output-path fence, so its declared 'test' deliverable reads as never produced and fails the build." '\n'
        '2. SOURCE LOCATION — put EVERY source module you author UNDER `src/ExprCalc/`. A source file placed outside `src/ExprCalc/` is dropped by the output-path fence.' '\n'
        '3. HARNESS-OWNED SCAFFOLD — the dependency manifest, the lockfile, and the test-runner / toolchain config files are created by the harness scaffold, and the verify command is fixed. Do NOT author or declare a runner/tool config file among your outputs — these are already provided: `src/ExprCalc/ExprCalc.csproj`, `tests/ExprCalc.Tests/ExprCalc.Tests.csproj`, `ExprCalc.sln`. A config file you emit is dropped by the output-path fence (and never changes how verify runs).'
    ),
}


@pytest.mark.parametrize("lang", sorted(_GOLDEN_LAYOUT_CONTRACT))
def test_golden_single_root_render_unchanged(lang):
    # The no-op proof: every single-root stack renders BYTE-FOR-BYTE as before the
    # multi-root fix. Passes on BOTH pre-fix and post-fix code.
    profile = resolve_layout_profile(
        language=lang,
        project_name="ExprCalc",
        source_dirs=["src"],
        test_dirs=["tests"],
    )
    assert profile is not None
    assert render_layout_placement_contract(profile) == _GOLDEN_LAYOUT_CONTRACT[lang]


def test_go_still_renders_empty():
    # Go has no resolved layout → None → "" (strict no-op, unchanged by the fix).
    profile = resolve_layout_profile(
        language="go",
        project_name="ExprCalc",
        source_dirs=["src"],
        test_dirs=["tests"],
    )
    assert profile is None
    assert render_layout_placement_contract(profile) == ""


def test_identical_normalized_roots_falls_to_single_root():
    # A synthetic profile whose source sets normalize to the SAME root must take
    # the single-root branch (>1 DISTINCT normalized root is the gate), and must
    # NOT emit a reference-form rule even though one placement declares
    # reference_base — byte-identical to a bare single-root profile.
    from codd.project_types import SourcePlacementSpec

    profile = LayoutProfile(
        language="x",
        package_name="app",
        source_root="src",
        package_root="src",
        test_root="tests",
        test_import_policy="relative",
        requires_package_init=False,
        requires_test_init=False,
        source_placements=(
            SourcePlacementSpec(root="src", file_globs=("src/**/*.x",)),
            # Same root after normalization (trailing slash) → collapses to one.
            SourcePlacementSpec(
                root="src/", file_globs=("src/**/*.y",), reference_base=True
            ),
        ),
    )
    block = render_layout_placement_contract(profile)
    baseline = LayoutProfile(
        language="x",
        package_name="app",
        source_root="src",
        package_root="src",
        test_root="tests",
        test_import_policy="relative",
        requires_package_init=False,
        requires_test_init=False,
    )
    assert block == render_layout_placement_contract(baseline)
    assert "owns MORE than one source root" not in block
    assert "SOURCE REFERENCE FORM" not in block
    assert "put EVERY source module you author UNDER `src/`" in block


def test_source_placement_key_is_additive_in_to_dict():
    # to_dict gains an additive `source_placements` key (a strict-key reader in the
    # full suite would catch a regression); an empty legacy profile serializes it
    # as [], cpp serializes both placements.
    empty = _js_like_profile().to_dict()
    assert empty["source_placements"] == []
    cpp_dict = _cpp_profile().to_dict()
    roots = {p["root"] for p in cpp_dict["source_placements"]}
    assert roots == {"src", "include"}
