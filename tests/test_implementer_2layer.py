from __future__ import annotations

from pathlib import Path
import re
import subprocess

import yaml

import codd.implementer as implementer_module
from codd.implementer import implement_tasks
from codd.llm.impl_step_deriver import ImplStep, ImplStepCacheRecord, impl_step_cache_path, write_impl_step_cache


DESIGN_NODE = "docs/design/contract.md"


def _write_doc(project: Path, relative_path: str, *, node_id: str, body: str) -> None:
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ncodd:\n  node_id: {node_id}\n  type: design\n---\n\n{body.rstrip()}\n",
        encoding="utf-8",
    )


def _setup_project(tmp_path: Path, *, config_extra: dict | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "codd").mkdir()
    config = {
        "project": {"name": "demo", "language": "python"},
        "ai_command": "mock-ai --print",
        "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/design/"], "config_files": [], "exclude": []},
    }
    if config_extra:
        config.update(config_extra)
    (project / "codd" / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    _write_doc(project, DESIGN_NODE, node_id="design:contract", body="# Contract\n\nDeclared behavior.\n")
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
        impl_step_cache_path(DESIGN_NODE, {"project_root": project}),
        ImplStepCacheRecord("fake", "key", DESIGN_NODE, "doc", "template", "now", [DESIGN_NODE], [explicit, implicit]),
    )


def _patch_ai(monkeypatch):
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        match = re.search(r"Output paths: (?P<output>[^\n,]+)", input)
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

    results = implement_tasks(project, design=DESIGN_NODE, output_paths=["src/contract"])

    assert results[0].error is None
    prompt = calls[0]
    assert "Implementation steps to follow" in prompt
    assert "[Layer 1 - Explicit, from design]" in prompt
    assert "[Layer 2 - Inferred, best-practice augment]" in prompt
    assert "build_declared_contract" in prompt
    assert "complete_related_concern" in prompt


def test_implementer_no_use_derived_steps_preserves_plain_prompt(tmp_path: Path, monkeypatch):
    project = _setup_project(tmp_path, config_extra={"implementer": {"use_derived_steps": True}})
    _write_step_cache(project, implicit_approved=True)
    calls = _patch_ai(monkeypatch)

    results = implement_tasks(project, design=DESIGN_NODE, output_paths=["src/contract"], use_derived_steps=False)

    assert results[0].error is None
    assert "Implementation steps to follow" not in calls[0]


def test_layer2_default_required_excludes_unapproved_inferred_steps(tmp_path: Path, monkeypatch):
    project = _setup_project(tmp_path, config_extra={"implementer": {"use_derived_steps": True}})
    _write_step_cache(project)
    calls = _patch_ai(monkeypatch)

    results = implement_tasks(project, design=DESIGN_NODE, output_paths=["src/contract"])

    assert results[0].error is None
    prompt = calls[0]
    assert "build_declared_contract" in prompt
    assert "complete_related_concern" not in prompt
