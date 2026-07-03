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
