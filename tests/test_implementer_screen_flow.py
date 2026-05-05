"""Tests for screen-flow prompt injection during implementation."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest
import yaml

import codd.implementer as implementer_module
from codd.cli import CoddCLIError
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
    codd = {"node_id": node_id, "type": doc_type}
    if depends_on is not None:
        codd["depends_on"] = depends_on

    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"{yaml.safe_dump({'codd': codd}, sort_keys=False, allow_unicode=True)}"
        "---\n\n"
        f"{body.rstrip()}\n",
        encoding="utf-8",
    )


def _setup_project(
    tmp_path: Path,
    *,
    task_title: str,
    module_hint: str = "app/login/page.tsx",
    deliverable: str = "UI route",
    extra_task_context: str = "",
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "codd").mkdir()
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "typescript"},
                "ai_command": "mock-ai --print",
                "scan": {
                    "source_dirs": ["src/"],
                    "doc_dirs": ["docs/plan/"],
                    "config_files": [],
                    "exclude": [],
                },
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (project / "DESIGN.md").write_text(
        """---
colors:
  primary: "#0f62fe"
---

# Demo Design
""",
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

{extra_task_context}
""",
    )
    return project


def _write_screen_flow(project: Path, content: str = "# Login\n- /login\n") -> None:
    path = project / "docs" / "extracted" / "screen-flow.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mock_ai(monkeypatch: pytest.MonkeyPatch, stdout: str | None = None) -> list[str]:
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        match = re.search(r"Output directory: (?P<output>src/generated/[^\n]+)", input)
        output_dir = match.group("output") if match else "src/generated/fallback"
        generated = stdout
        if generated is None:
            generated = (
                f"=== FILE: {output_dir}/index.tsx ===\n"
                "```tsx\n"
                "export const Page = () => null;\n"
                "```\n"
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=generated, stderr="")

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


def _plan_and_task():
    plan = implementer_module.ImplementationPlan(
        node_id="plan:test",
        path=Path("docs/plan/implementation_plan.md"),
        content="# Plan",
        depends_on=[],
        conventions=[],
    )
    task = implementer_module.ImplementationTask(
        task_id="1-1",
        title="Build login page",
        summary="Login screen",
        module_hint="app/login/page.tsx",
        deliverable="Route UI",
        output_dir="src/generated/login",
        dependency_node_ids=[],
        task_context="Implement /login",
    )
    return plan, task


def test_load_screen_flow_found(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _write_screen_flow(project, "# Screens\n- /login\n")

    assert implementer_module._load_screen_flow_for_implementation(project) == "# Screens\n- /login\n"


def test_load_screen_flow_not_found(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()

    with pytest.warns(UserWarning, match="screen-flow.md not found"):
        assert implementer_module._load_screen_flow_for_implementation(project) is None


def test_build_prompt_includes_screen_flow():
    plan, task = _plan_and_task()

    prompt = implementer_module._build_implementation_prompt(
        config={"project": {"name": "demo", "language": "typescript"}},
        plan=plan,
        task=task,
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        screen_flow_content="# Screens\n- /login\n",
        screen_flow_routes=["/login"],
    )

    assert "--- SCREEN-FLOW (UI ROUTE DEFINITIONS) ---" in prompt
    assert "This UI task must implement the relevant route(s):" in prompt
    assert "- /login" in prompt
    assert "--- END SCREEN-FLOW ---" in prompt


def test_build_prompt_no_screen_flow():
    plan, task = _plan_and_task()

    prompt = implementer_module._build_implementation_prompt(
        config={"project": {"name": "demo", "language": "typescript"}},
        plan=plan,
        task=task,
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "SCREEN-FLOW" not in prompt


def test_is_ui_task_page_keyword():
    assert implementer_module._is_ui_task("Build settings page")


def test_is_ui_task_login_keyword():
    assert implementer_module._is_ui_task("ログインフォームを実装")


def test_is_ui_task_non_ui():
    assert not implementer_module._is_ui_task("Build billing service")


def test_zero_files_raises_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _setup_project(
        tmp_path,
        task_title="Build billing service",
        module_hint="lib/billing/service.ts",
        deliverable="Domain service",
    )
    _mock_ai(monkeypatch, stdout="")

    with pytest.raises(CoddCLIError, match="produced 0 generated files"):
        implement_tasks(project, task="1-1")


def test_zero_files_with_skip_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _setup_project(
        tmp_path,
        task_title="Document manual migration",
        module_hint="docs/migration.md",
        deliverable="No generated files",
        extra_task_context="skip_generation: true",
    )
    _mock_ai(monkeypatch, stdout="")

    results = implement_tasks(project, task="1-1")

    assert len(results) == 1
    assert results[0].generated_files == []
    assert results[0].error is None


def test_ui_task_includes_route_in_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _setup_project(tmp_path, task_title="Build login page")
    _write_screen_flow(project, "# Routes\n- /\n- /login\n- /dashboard\n")
    calls = _mock_ai(monkeypatch)

    implement_tasks(project, task="1-1")

    assert "--- SCREEN-FLOW (UI ROUTE DEFINITIONS) ---" in calls[0]
    assert "This UI task must implement the relevant route(s):" in calls[0]
    assert "- /login" in calls[0]


def test_screen_flow_truncated_at_8000_chars():
    plan, task = _plan_and_task()
    screen_flow = "A" * 8000 + "TAIL"

    prompt = implementer_module._build_implementation_prompt(
        config={"project": {"name": "demo", "language": "typescript"}},
        plan=plan,
        task=task,
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        screen_flow_content=screen_flow,
    )

    assert "A" * 8000 in prompt
    assert "TAIL" not in prompt


def test_generality_no_framework_specific_code():
    framework_specific = {"react", "nextjs", "next.js", "vue", "svelte", "nuxt", "angular"}

    assert implementer_module._UI_TASK_KEYWORDS.isdisjoint(framework_specific)
