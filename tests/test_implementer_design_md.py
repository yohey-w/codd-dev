"""Tests for DESIGN.md prompt injection during implementation."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest
import yaml

import codd.implementer as implementer_module
from codd.implementer import implement_tasks


def _write_doc(
    project: Path,
    relative_path: str,
    *,
    node_id: str,
    doc_type: str,
    body: str,
    depends_on: list[dict] | None = None,
) -> None:
    payload = {"codd": {"node_id": node_id, "type": doc_type}}
    if depends_on is not None:
        payload["codd"]["depends_on"] = depends_on

    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n{yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)}---\n\n{body.rstrip()}\n",
        encoding="utf-8",
    )


def _setup_project(
    tmp_path: Path,
    *,
    task_title: str,
    module_hint: str,
    deliverable: str,
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "typescript"},
                "ai_command": "mock-ai --print",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_doc(
        project,
        "docs/plan/implementation_plan.md",
        node_id="plan:implementation-plan",
        doc_type="plan",
        depends_on=[],
        body=f"""# Implementation Plan

#### Sprint 1: Demo

| # | 作業項目 | 対応モジュール | 成果物 |
|---|---|---|---|
| 1-1 | {task_title} | {module_hint} | {deliverable} |
""",
    )
    return project


def _write_design_md(project: Path, content: str | None = None) -> None:
    (project / "DESIGN.md").write_text(
        content
        or """---
colors:
  primary: "#0f62fe"
spacing:
  md: 16px
---

# Demo Design
""",
        encoding="utf-8",
    )


def _mock_ai(monkeypatch: pytest.MonkeyPatch, extension: str) -> list[str]:
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check):
        match = re.search(r"Output directory: (?P<output>src/generated/[^\n]+)", input)
        assert match is not None
        output_dir = match.group("output")
        calls.append(input)
        code_fence = "tsx" if extension == ".tsx" else "ts"
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                f"=== FILE: {output_dir}/index{extension} ===\n"
                f"```{code_fence}\n"
                "export const ready = true;\n"
                "```\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


def test_ui_file_triggers_design_md_injection(tmp_path, monkeypatch):
    project = _setup_project(
        tmp_path,
        task_title="Build dashboard component",
        module_hint="components/Dashboard.tsx",
        deliverable="Dashboard UI component",
    )
    _write_design_md(project)
    calls = _mock_ai(monkeypatch, ".tsx")

    implement_tasks(project, task="1-1")

    assert "# DESIGN.md tokens (W3C Design Tokens spec)" in calls[0]
    assert "- colors.primary (color): #0f62fe" in calls[0]
    assert "- spacing.md (spacing): 16px" in calls[0]


def test_non_ui_file_no_injection(tmp_path, monkeypatch):
    project = _setup_project(
        tmp_path,
        task_title="Build domain service",
        module_hint="lib/domain/service.ts",
        deliverable="Domain service",
    )
    _write_design_md(project)
    calls = _mock_ai(monkeypatch, ".ts")

    implement_tasks(project, task="1-1")

    assert "DESIGN.md tokens" not in calls[0]
    assert "colors.primary" not in calls[0]


def test_missing_design_md_warns(tmp_path, monkeypatch):
    project = _setup_project(
        tmp_path,
        task_title="Build dashboard component",
        module_hint="components/Dashboard.tsx",
        deliverable="Dashboard UI component",
    )
    _mock_ai(monkeypatch, ".tsx")

    with pytest.warns(UserWarning, match="DESIGN.md not found"):
        implement_tasks(project, task="1-1")


def test_design_md_parse_error_warns(tmp_path, monkeypatch):
    project = _setup_project(
        tmp_path,
        task_title="Build dashboard component",
        module_hint="components/Dashboard.tsx",
        deliverable="Dashboard UI component",
    )
    _write_design_md(project, "---\ncolors: [\n---\n# Broken\n")
    _mock_ai(monkeypatch, ".tsx")

    with pytest.warns(UserWarning, match="DESIGN.md parse error"):
        implement_tasks(project, task="1-1")
