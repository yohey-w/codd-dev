from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.config import load_project_config
from codd.implementer import auto_detect_task
from codd.implementer import _extract_all_tasks, _load_implementation_plan
from codd.llm.plan_deriver import DerivedTask, DerivedTaskCacheRecord, write_derived_task_cache


def _write_project(tmp_path: Path, task_count: int, *, with_plan: bool = True) -> Path:
    project = tmp_path / "project"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "python"},
                "ai_command": "fake-ai --run",
                "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/plan/"], "config_files": [], "exclude": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    if not with_plan:
        return project

    rows = ["| # | Task | Module | Deliverable |", "|---|---|---|---|"]
    for index in range(1, task_count + 1):
        rows.append(f"| 1-{index} | Build service {index} | src/service_{index}.py | service {index} |")
    plan_path = project / "docs" / "plan" / "implementation_plan.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "---\n"
        "codd:\n"
        '  node_id: "plan:implementation-plan"\n'
        '  type: "plan"\n'
        "---\n\n"
        "# Implementation Plan\n\n"
        "#### Sprint 1: Services\n\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    return project


def _tasks(project: Path):
    config = load_project_config(project)
    return _extract_all_tasks(_load_implementation_plan(project, config))


def _mark_generated(project: Path, task_index: int) -> None:
    task = _tasks(project)[task_index]
    output = project / task.output_dir / "index.py"
    output.parent.mkdir(parents=True)
    output.write_text("generated = True\n", encoding="utf-8")


def _write_derived_cache(project: Path, *task_ids: str) -> None:
    tasks = [
        DerivedTask.from_dict(
            {
                "id": task_id,
                "title": task_id.replace("_", " ").title(),
                "description": "Implement derived task.",
                "source_design_doc": "docs/design/contract.md",
                "v_model_layer": "detailed",
                "approved": True,
            }
        )
        for task_id in task_ids
    ]
    write_derived_task_cache(
        project / ".codd" / "derived_tasks" / "docs_design_contract.md.yaml",
        DerivedTaskCacheRecord("fake", "key", "doc", "template", "now", ["docs/design/contract.md"], tasks),
    )


def test_auto_detect_single_unfinished_plan_task(tmp_path: Path):
    project = _write_project(tmp_path, 1)

    assert auto_detect_task(project) == "1-1"


def test_auto_detect_ignores_tasks_with_generated_output(tmp_path: Path):
    project = _write_project(tmp_path, 2)
    _mark_generated(project, 0)

    assert auto_detect_task(project) == "1-2"


def test_auto_detect_requires_explicit_task_for_multiple_plan_candidates(tmp_path: Path):
    project = _write_project(tmp_path, 2)

    with pytest.raises(ValueError, match="multiple implementation task candidates"):
        auto_detect_task(project)


def test_auto_detect_falls_back_to_latest_approved_derived_task(tmp_path: Path):
    project = _write_project(tmp_path, 0, with_plan=False)
    _write_derived_cache(project, "derived_service")

    assert auto_detect_task(project) == "derived_service"
