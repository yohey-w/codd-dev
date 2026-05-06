from __future__ import annotations

from pathlib import Path
import re
import subprocess

import yaml

import codd.implementer as implementer_module
from codd.implementer import implement_tasks
from codd.llm.impl_step_deriver import ImplStep, ImplStepCacheRecord, impl_step_cache_path, write_impl_step_cache


def _write_doc(project: Path, relative_path: str, *, node_id: str, doc_type: str, body: str, depends_on: list[dict] | None = None):
    doc_path = project / relative_path
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    codd = {"node_id": node_id, "type": doc_type}
    if depends_on is not None:
        codd["depends_on"] = depends_on
    doc_path.write_text(
        f"---\n{yaml.safe_dump({'codd': codd}, sort_keys=False)}---\n\n{body.rstrip()}\n",
        encoding="utf-8",
    )


def _setup_project(tmp_path: Path, *, config_extra: dict | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    config = {
        "project": {"name": "demo", "language": "python"},
        "ai_command": "mock-ai --print",
        "scan": {
            "source_dirs": ["src/"],
            "doc_dirs": ["docs/design/", "docs/plan/"],
            "test_dirs": ["tests/"],
            "config_files": [],
            "exclude": [],
        },
    }
    if config_extra:
        config.update(config_extra)
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    _write_doc(
        project,
        "docs/design/contract.md",
        node_id="design:contract",
        doc_type="design",
        body="# Contract\n\nDeclared behavior.\n",
    )
    _write_doc(
        project,
        "docs/plan/implementation_plan.md",
        node_id="plan:implementation-plan",
        doc_type="plan",
        depends_on=[{"id": "design:contract", "relation": "depends_on"}],
        body="""# Implementation Plan

#### Sprint 1: Contract

| # | 作業項目 | 対応モジュール | 成果物 |
|---|---|---|---|
| 1-1 | Build contract | src/contract.py | Contract |
""",
    )
    return project


def _write_step_cache(project: Path, *, implicit_approved: bool = False) -> None:
    explicit = ImplStep.from_dict(
        {
            "id": "build_declared_contract",
            "kind": "contract_builder",
            "rationale": "Build declared behavior.",
            "source_design_section": "docs/design/contract.md",
            "expected_outputs": ["src/contract.py"],
            "approved": True,
        }
    )
    implicit = ImplStep.from_dict(
        {
            "id": "complete_related_concern",
            "kind": "related_completion",
            "rationale": "Complete inferred related concern.",
            "source_design_section": "best_practice_augmenter",
            "expected_outputs": ["src/related.py"],
            "inferred": True,
            "confidence": 0.95,
            "best_practice_category": "completion",
            "approved": implicit_approved,
        }
    )
    write_impl_step_cache(
        impl_step_cache_path("1-1", {"project_root": project}),
        ImplStepCacheRecord("fake", "key", "1-1", "doc", "template", "now", ["docs/design/contract.md"], [explicit, implicit]),
    )


def _patch_ai(monkeypatch):
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        match = re.search(r"Output directory: (?P<output>src/generated/[^\n]+)", input)
        assert match is not None
        output_dir = match.group("output")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                f"=== FILE: {output_dir}/service.py ===\n"
                "```python\n"
                "def build_service() -> bool:\n"
                "    return True\n"
                "```\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


def test_implementer_injects_approved_layer1_and_auto_high_confidence_layer2(tmp_path: Path, monkeypatch):
    project = _setup_project(
        tmp_path,
        config_extra={
            "implementer": {
                "use_derived_steps": True,
                "approval_mode_per_step_kind": {"contract_builder": "required"},
                "layer_2_approval_mode": {
                    "mode": "auto_high_confidence_only",
                    "require_explicit_optin": True,
                    "confidence_threshold": 0.9,
                },
            }
        },
    )
    _write_step_cache(project)
    calls = _patch_ai(monkeypatch)

    results = implement_tasks(project, task="1-1")

    assert results[0].error is None
    prompt = calls[0]
    assert "Implementation steps to follow" in prompt
    assert "[Layer 1 - Explicit, from design]" in prompt
    assert "[Layer 2 - Inferred, best-practice augment]" in prompt
    assert "build_declared_contract" in prompt
    assert "complete_related_concern" in prompt


def test_implementer_no_use_derived_steps_preserves_legacy_prompt(tmp_path: Path, monkeypatch):
    project = _setup_project(tmp_path, config_extra={"implementer": {"use_derived_steps": True}})
    _write_step_cache(project, implicit_approved=True)
    calls = _patch_ai(monkeypatch)

    results = implement_tasks(project, task="1-1", use_derived_steps=False)

    assert results[0].error is None
    assert "Implementation steps to follow" not in calls[0]


def test_layer2_default_required_excludes_unapproved_inferred_steps(tmp_path: Path, monkeypatch):
    project = _setup_project(tmp_path, config_extra={"implementer": {"use_derived_steps": True}})
    _write_step_cache(project)
    calls = _patch_ai(monkeypatch)

    results = implement_tasks(project, task="1-1")

    assert results[0].error is None
    prompt = calls[0]
    assert "build_declared_contract" in prompt
    assert "complete_related_concern" not in prompt
