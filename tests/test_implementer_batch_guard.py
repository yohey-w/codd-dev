from __future__ import annotations

from pathlib import Path
import re
import subprocess

from click.testing import CliRunner
import pytest
import yaml

import codd.implementer as implementer_module
from codd.cli import main
from codd.implementer import implement_tasks


def _write_project(tmp_path: Path, wave_counts: dict[int, int]) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
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
        ),
        encoding="utf-8",
    )

    plan_lines = ["# Implementation Plan", "", "## 2. Milestones", ""]
    for wave, count in wave_counts.items():
        plan_lines.append(f"### Phase {wave}")
        plan_lines.append("")
        for task_number in range(1, count + 1):
            plan_lines.append(f"#### M{wave}.{task_number} Task {wave}-{task_number}")
            plan_lines.append("")
            plan_lines.append(f"Implement task {wave}-{task_number}.")
            plan_lines.append("")

    plan_path = project / "docs" / "plan" / "implementation_plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "---\n"
        "codd:\n"
        '  node_id: "plan:implementation-plan"\n'
        '  type: "plan"\n'
        "---\n\n"
        + "\n".join(plan_lines),
        encoding="utf-8",
    )
    return project


def _mock_ai(monkeypatch: pytest.MonkeyPatch) -> list[str]:
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
                f"=== FILE: {output_dir}/index.ts ===\n"
                "```ts\n"
                "export const generated = true;\n"
                "```\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


def test_max_tasks_default_is_30(tmp_path: Path):
    project = _write_project(tmp_path, {1: 31})

    with pytest.raises(ValueError) as exc_info:
        implement_tasks(project)

    message = str(exc_info.value)
    assert "Plan contains 31 tasks" in message
    assert "--max-tasks=30" in message
    assert "codd implement --wave WAVE_ID" in message
    assert "codd implement --task TASK_ID" in message


def test_max_tasks_guard_runs_before_clean(tmp_path: Path):
    project = _write_project(tmp_path, {1: 31})
    generated_file = project / "src" / "generated" / "existing" / "index.ts"
    generated_file.parent.mkdir(parents=True)
    generated_file.write_text("export const existing = true;\n", encoding="utf-8")

    with pytest.raises(ValueError):
        implement_tasks(project, clean=True)

    assert generated_file.exists()


def test_max_tasks_explicit_allows_large(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _write_project(tmp_path, {1: 106})
    calls = _mock_ai(monkeypatch)

    results = implement_tasks(project, max_tasks=106)

    assert len(results) == 106
    assert len(calls) == 106
    assert all(result.error is None for result in results)


def test_wave_filter_reduces_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _write_project(tmp_path, {1: 2, 2: 3})
    calls = _mock_ai(monkeypatch)

    results = implement_tasks(project, wave=1)

    assert [result.task_id for result in results] == ["m1.1", "m1.2"]
    assert len(calls) == 2


def test_wave_filter_unknown_wave(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _write_project(tmp_path, {1: 2})
    calls = _mock_ai(monkeypatch)

    results = implement_tasks(project, wave=99)

    assert results == []
    assert calls == []


def test_task_option_bypasses_max_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _write_project(tmp_path, {1: 31})
    _mock_ai(monkeypatch)

    results = implement_tasks(project, task="m1.31", max_tasks=1)

    assert [result.task_id for result in results] == ["m1.31"]


def test_implement_cli_passes_batch_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _write_project(tmp_path, {1: 1})
    captured: dict[str, object] = {}

    def fake_implement_tasks(project_root, *, task, ai_command, clean, max_tasks, wave):
        captured.update(
            {
                "project_root": project_root,
                "task": task,
                "ai_command": ai_command,
                "clean": clean,
                "max_tasks": max_tasks,
                "wave": wave,
            }
        )
        return []

    monkeypatch.setattr(implementer_module, "implement_tasks", fake_implement_tasks)

    result = CliRunner().invoke(
        main,
        [
            "implement",
            "--path",
            str(project),
            "--max-tasks",
            "7",
            "--wave",
            "2",
            "--task",
            "m2.1",
            "--ai-cmd",
            "custom-ai --print",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "project_root": project.resolve(),
        "task": "m2.1",
        "ai_command": "custom-ai --print",
        "clean": False,
        "max_tasks": 7,
        "wave": 2,
    }
