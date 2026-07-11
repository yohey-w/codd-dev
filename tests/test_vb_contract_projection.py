"""Tests for VB contract projection into the implement/generation prompts.

The marker-authenticity gate rejects `codd: covers vb=<id>` markers that name
an id no VB table declares (orphans) or that sit on assertion-less tests. Before
this change the implement prompt stated the marker RULES but never enumerated
the CLOSED set of declared VB ids, so the model invented ids (acceptance-
criterion ids like ``AC-10`` leaking into ``vb=`` markers; descriptive
inventions like ``VB-TOK-NONZERO-POSITION``) that the gate then rejected with no
way to prevent it up front. These tests pin the projection: the SAME declared
set the gate reconciles against is rendered into the prompt as a closed list,
with the assertion-quality + coverage-is-completion rules.
"""

from __future__ import annotations

from pathlib import Path

from codd.generator import _resolve_layout_placement_contract
from codd.implementer import _build_implementation_prompt
from codd.implementer import DesignContext, ImplementSpec
from codd.languages import resolve_assertion_guidance
from codd.verifiable_behavior_audit import (
    collect_declared_vb_ids,
    load_verifiable_behaviors,
    render_vb_contract,
    _normalize_vb_id,
)


_VB_DOC = """---
codd:
  node_id: test:test-strategy
---

# Test Strategy

| VB | Description | Test |
| --- | --- | --- |
| VB-01 | tokenize returns ordered tokens | test_tokenize |
| VB-02 | unknown char raises LexicalError | test_unknown |
| VB-AUTH-03 | login rejects a missing id | test_login_missing |
"""


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project:\n  name: demo\n  language: python\nscan:\n  test_dirs: [tests/]\n",
        encoding="utf-8",
    )
    doc = project / "docs" / "test" / "test_strategy.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(_VB_DOC, encoding="utf-8")
    return project


# ---------------------------------------------------------------------------
# collect_declared_vb_ids — behavior-invariant projection of the truth source
# ---------------------------------------------------------------------------


def test_collect_declared_vb_ids_matches_gate_declared_set(tmp_path):
    project = _make_project(tmp_path)
    declared = collect_declared_vb_ids(project)
    ids = {_normalize_vb_id(b.vb_id) for b in declared}
    # Same set the gate builds from load_verifiable_behaviors (single truth source).
    gate_ids = {_normalize_vb_id(b.vb_id) for b in load_verifiable_behaviors(project)}
    assert ids == gate_ids
    assert {b.vb_id for b in declared} == {"VB-01", "VB-02", "VB-AUTH-03"}


def test_collect_declared_vb_ids_empty_project(tmp_path):
    project = tmp_path / "empty"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project:\n  name: e\n  language: python\n", encoding="utf-8"
    )
    assert collect_declared_vb_ids(project) == []


# ---------------------------------------------------------------------------
# render_vb_contract — closed list + 3 rules, language-free
# ---------------------------------------------------------------------------


def test_render_vb_contract_enumerates_closed_list_and_rules(tmp_path):
    project = _make_project(tmp_path)
    block = render_vb_contract(collect_declared_vb_ids(project))
    # (1) closed id list — every declared id appears with its description.
    assert "CLOSED ID LIST" in block
    assert "VB-01" in block and "VB-02" in block and "VB-AUTH-03" in block
    assert "tokenize returns ordered tokens" in block
    assert "test_strategy.md" in block  # source doc named
    # orphan-prevention: names the acceptance-criterion failure mode explicitly.
    assert "AC-10" in block or "acceptance-criterion" in block
    # (2) assertion quality — observed result, no self-comparison.
    assert "ASSERTION QUALITY" in block
    assert "OBSERVED" in block
    # (3) coverage is completion.
    assert "COMPLETION CONDITION" in block


def test_render_vb_contract_is_language_free():
    behaviors = load_verifiable_behaviors  # noqa: F841 (referenced for clarity)
    from codd.verifiable_behavior_audit import VerifiableBehavior

    block = render_vb_contract(
        [VerifiableBehavior(vb_id="VB-01", description="d", source_doc="docs/test/x.md")]
    )
    # No framework/language tokens in the shared contract text.
    for token in ("pytest", "vitest", "jest", "junit", "python", "typescript", "def ", "import "):
        assert token.lower() not in block.lower(), token


def test_render_vb_contract_empty_returns_empty():
    assert render_vb_contract([]) == ""


def test_render_vb_contract_appends_extra_guidance():
    from codd.verifiable_behavior_audit import VerifiableBehavior

    marker = "PYTEST-IDIOM-SENTINEL-9f3a"
    block = render_vb_contract(
        [VerifiableBehavior(vb_id="VB-01", description="d", source_doc="x.md")],
        extra_guidance=f"Use {marker} here.",
    )
    assert marker in block
    # Absent extra_guidance ⇒ nothing appended (no non-opt-in default).
    block2 = render_vb_contract(
        [VerifiableBehavior(vb_id="VB-01", description="d", source_doc="x.md")]
    )
    assert marker not in block2


# ---------------------------------------------------------------------------
# resolve_assertion_guidance — opt-in per-language, absent ⇒ None
# ---------------------------------------------------------------------------


def test_resolve_assertion_guidance_absent_is_none():
    # No shipped profile opts in yet — every language returns None (append nothing).
    assert resolve_assertion_guidance("python") is None
    assert resolve_assertion_guidance("typescript") is None
    assert resolve_assertion_guidance(None) is None
    assert resolve_assertion_guidance("no-such-language") is None


def test_loader_parses_assertion_guidance():
    from codd.languages.loader import _parse_tests

    spec = _parse_tests({"tests": {"assertion_guidance": "idiom text here"}})
    assert spec is not None
    assert spec.assertion_guidance == "idiom text here"
    # Absent ⇒ None.
    spec2 = _parse_tests({"tests": {"test_file_globs": ["tests/**"]}})
    assert spec2 is not None
    assert spec2.assertion_guidance is None


# ---------------------------------------------------------------------------
# Implement prompt injection — only for test-scope tasks, only with a VB table
# ---------------------------------------------------------------------------


def test_implement_prompt_injects_contract_for_test_scope_task(tmp_path):
    project = _make_project(tmp_path)
    prompt = _build_implementation_prompt(
        config={
            "project": {"name": "demo", "language": "python"},
            "scan": {"test_dirs": ["tests/"]},
        },
        design_context=DesignContext(
            node_id="test:test-strategy",
            path=Path("docs/test/test_strategy.md"),
            content="# Test Strategy\n",
        ),
        spec=ImplementSpec("test:test-strategy", ["tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )
    assert "Verifiable-behavior CONTRACT" in prompt
    assert "CLOSED ID LIST" in prompt
    assert "VB-AUTH-03" in prompt  # the actual declared id is enumerated


def test_implement_prompt_omits_contract_for_source_scope_task(tmp_path):
    project = _make_project(tmp_path)
    prompt = _build_implementation_prompt(
        config={
            "project": {"name": "demo", "language": "python"},
            "scan": {"test_dirs": ["tests/"]},
        },
        design_context=DesignContext(
            node_id="design:system",
            path=Path("docs/design/system_design.md"),
            content="# System Design\n",
        ),
        spec=ImplementSpec("design:system", ["src"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )
    assert "Verifiable-behavior CONTRACT" not in prompt


def test_implement_prompt_omits_contract_without_project_root():
    # A DI/standalone caller with no project_root cannot enumerate the list;
    # it degrades to the prior abstract marker rules, never crashes.
    prompt = _build_implementation_prompt(
        config={
            "project": {"name": "demo", "language": "python"},
            "scan": {"test_dirs": ["tests/"]},
        },
        design_context=DesignContext(
            node_id="test:test-strategy",
            path=Path("docs/test/test_strategy.md"),
            content="# Test Strategy\n",
        ),
        spec=ImplementSpec("test:test-strategy", ["tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )
    assert "Verifiable-behavior CONTRACT" not in prompt


# ---------------------------------------------------------------------------
# Generator canonical VB-table hygiene — immutability + observable-surface rules
# ---------------------------------------------------------------------------


def test_generator_canonical_vb_head_has_hygiene_rules():
    from codd.generator import _TEST_DOC_CANONICAL_VB_HEAD

    text = "\n".join(_TEST_DOC_CANONICAL_VB_HEAD)
    assert "IMMUTABLE" in text
    assert "OBSERVABLE" in text
    assert "public" in text.lower()


# ---------------------------------------------------------------------------
# resolve_namespace_guidance — opt-in per-language namespace-coherence contract
# (csharp4 exprcalc dogfood 2026-07-11: impl declared `namespace ExprCalc.Evaluator;
#  public static class Evaluator` — a namespace segment sharing a TYPE's name —
#  so tests under `using ExprCalc;` resolved `Evaluator` to the NAMESPACE and
#  every call failed CS0234 ×34. The convention must be pinned at generation;
#  the native oracle stays the enforcing gate.)
# ---------------------------------------------------------------------------


def test_resolve_namespace_guidance_csharp_pins_single_root_namespace():
    from codd.languages import resolve_namespace_guidance

    text = resolve_namespace_guidance("csharp", package_name="ExprCalc")
    assert text is not None
    assert "namespace ExprCalc;" in text
    assert "shadow" in text.lower()
    assert "{package_name}" not in text  # substituted


def test_resolve_namespace_guidance_absent_is_none():
    from codd.languages import resolve_namespace_guidance

    assert resolve_namespace_guidance("python") is None
    assert resolve_namespace_guidance("typescript") is None
    assert resolve_namespace_guidance(None) is None
    assert resolve_namespace_guidance("no-such-language") is None


# ---------------------------------------------------------------------------
# resolve_module_specifier_guidance — opt-in per-language module-specifier
# coherence contract (S3 StockRoom-mini TS greenfield dogfood 2026-07-12: under
# `moduleResolution: NodeNext`, independently-generated files split on relative-
# import specifiers — some emitted `./x.js` (correct), some `./x` → TS2835 ×30.
# The convention must be pinned at generation; the native oracle (typecheck)
# stays the enforcing gate. Same class as the C# namespace-coherence fix.)
# ---------------------------------------------------------------------------


def _make_ts_project(tmp_path: Path) -> Path:
    project = tmp_path / "tsproj"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project:\n  name: ExprCalc\n  language: typescript\n",
        encoding="utf-8",
    )
    return project


def test_resolve_module_specifier_guidance_typescript_pins_js_extension():
    from codd.languages import resolve_module_specifier_guidance

    text = resolve_module_specifier_guidance("typescript")
    assert text is not None
    assert ".js" in text
    assert "TS2835" in text
    assert "MODULE-SPECIFIER" in text.upper()


def test_resolve_module_specifier_guidance_absent_is_none():
    # Generality: a language whose profile does not declare the field returns
    # None (append nothing) — the contract is strictly opt-in via profile YAML.
    from codd.languages import resolve_module_specifier_guidance

    assert resolve_module_specifier_guidance("python") is None
    assert resolve_module_specifier_guidance("csharp") is None
    assert resolve_module_specifier_guidance(None) is None
    assert resolve_module_specifier_guidance("no-such-language") is None


def test_module_specifier_guidance_renders_into_generator_stage(tmp_path):
    # GENERATE-stage twin of the implement injection.
    project = _make_ts_project(tmp_path)
    block = _resolve_layout_placement_contract(
        {
            "project": {"name": "ExprCalc", "language": "typescript"},
            "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
        },
        project,
    )
    assert "TS2835" in block
    assert ".js" in block


def test_module_specifier_guidance_renders_into_implement_stage(tmp_path):
    project = _make_ts_project(tmp_path)
    prompt = _build_implementation_prompt(
        config={
            "project": {"name": "ExprCalc", "language": "typescript"},
            "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
        },
        design_context=DesignContext(
            node_id="design:system",
            path=Path("docs/design/system_design.md"),
            content="# System Design\n",
        ),
        spec=ImplementSpec("design:system", ["src"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )
    assert "TS2835" in prompt
    assert ".js" in prompt


def test_module_specifier_guidance_absent_language_renders_nothing(tmp_path):
    # Generality: a non-TS project injects NOTHING at either stage.
    project = tmp_path / "py"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project:\n  name: demo\n  language: python\n", encoding="utf-8"
    )
    config = {
        "project": {"name": "demo", "language": "python"},
        "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
    }
    gen_block = _resolve_layout_placement_contract(config, project)
    assert "TS2835" not in gen_block
    assert "MODULE-SPECIFIER COHERENCE" not in gen_block
    prompt = _build_implementation_prompt(
        config=config,
        design_context=DesignContext(
            node_id="design:system",
            path=Path("docs/design/system_design.md"),
            content="# System Design\n",
        ),
        spec=ImplementSpec("design:system", ["src"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )
    assert "TS2835" not in prompt
    assert "MODULE-SPECIFIER COHERENCE" not in prompt


# ---------------------------------------------------------------------------
# resolve_runtime_dependency_guidance — implement-phase RUNTIME-dep-declaration
# obligation (S3 StockRoom-mini TS greenfield dogfood 2026-07-12: the model
# imported third-party RUNTIME packages it legitimately chose — `import express`
# at src/core/http/server.ts, `import better-sqlite3` at src/core/db/client.ts —
# but neither was declared in `package.json`, so the implement-time typecheck
# could not resolve them → TS2307 ×2. The scaffold writes ONLY the harness-owned
# test toolchain into the manifest; the concrete runtime packages a design DEFERS
# to scaffold/implement-time are first known when the model writes the import, so
# the model must be told to declare its own region of the manifest. The manifest-
# acceptance pipeline already consumes a SUT-declared manifest end-to-end; this
# block is the missing obligation. Data-projected from the toolchain profile's
# manifest_filename — GENERIC across npm/pip/cargo, no framework/package hardcode.)
# ---------------------------------------------------------------------------


def test_resolve_runtime_dependency_guidance_projects_manifest_filename():
    from codd.languages import resolve_runtime_dependency_guidance
    from codd.project_types import resolve_layout_profile

    profile = resolve_layout_profile(language="typescript", project_name="ExprCalc")
    assert profile is not None and profile.toolchain_dependencies is not None
    text = resolve_runtime_dependency_guidance(profile.toolchain_dependencies)
    assert text is not None
    # The manifest filename is DATA-PROJECTED from the toolchain profile, never a
    # hardcoded literal — assert it is exactly the profile's own value.
    assert profile.toolchain_dependencies.manifest_filename in text  # "package.json"
    assert "runtime" in text.lower()
    assert "responsibility" in text.lower()


def test_resolve_runtime_dependency_guidance_absent_toolchain_is_none():
    # Generality: a manifest-less stack carries toolchain_dependencies=None, so
    # the caller passes None → append NOTHING (same opt-in guard as the other
    # prompt blocks). A profile whose manifest_filename is blank is also a no-op.
    from codd.languages import resolve_runtime_dependency_guidance

    assert resolve_runtime_dependency_guidance(None) is None


def test_runtime_dependency_guidance_renders_into_implement_stage(tmp_path):
    # A stack that declares a manifest (TS/npm → package.json) gets the runtime-
    # dependency-declaration obligation injected, with the manifest filename
    # data-projected in (not a hardcoded literal in the prompt builder).
    project = _make_ts_project(tmp_path)
    prompt = _build_implementation_prompt(
        config={
            "project": {"name": "ExprCalc", "language": "typescript"},
            "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
        },
        design_context=DesignContext(
            node_id="design:system",
            path=Path("docs/design/system_design.md"),
            content="# System Design\n",
        ),
        spec=ImplementSpec("design:system", ["src"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )
    assert "RUNTIME DEPENDENCY DECLARATION" in prompt
    assert "package.json" in prompt
    assert "responsibility" in prompt.lower()


def test_runtime_dependency_guidance_absent_language_renders_nothing(tmp_path):
    # Generality: a manifest-less project (Python — toolchain_dependencies is
    # None) injects NOTHING.
    project = tmp_path / "py"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project:\n  name: demo\n  language: python\n", encoding="utf-8"
    )
    prompt = _build_implementation_prompt(
        config={
            "project": {"name": "demo", "language": "python"},
            "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
        },
        design_context=DesignContext(
            node_id="design:system",
            path=Path("docs/design/system_design.md"),
            content="# System Design\n",
        ),
        spec=ImplementSpec("design:system", ["src"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )
    assert "RUNTIME DEPENDENCY DECLARATION" not in prompt
