from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.implementer import auto_detect_task
from codd.llm.plan_deriver import DerivedTask, DerivedTaskCacheRecord, write_derived_task_cache


def _write_project(tmp_path: Path, implement_mapping: dict[str, list[str]] | None = None) -> Path:
    project = tmp_path / "project"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    config: dict = {
        "project": {"name": "demo", "language": "python"},
        "ai_command": "fake-ai --run",
        "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/design/"], "config_files": [], "exclude": []},
    }
    if implement_mapping is not None:
        config["implement"] = {"default_output_paths": implement_mapping}
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return project


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


def test_auto_detect_single_configured_design_node(tmp_path: Path):
    project = _write_project(tmp_path, {"docs/design/auth.md": ["src/auth"]})

    assert auto_detect_task(project) == "docs/design/auth.md"


def test_auto_detect_requires_explicit_design_for_multiple_configured_nodes(tmp_path: Path):
    project = _write_project(
        tmp_path,
        {
            "docs/design/auth.md": ["src/auth"],
            "docs/design/billing.md": ["src/billing"],
        },
    )

    with pytest.raises(ValueError, match="could not auto-detect"):
        auto_detect_task(project)


def test_auto_detect_falls_back_to_latest_approved_derived_task(tmp_path: Path):
    project = _write_project(tmp_path)
    _write_derived_cache(project, "derived_service")

    assert auto_detect_task(project) == "derived_service"


def test_auto_detect_requires_explicit_design_for_multiple_derived_candidates(tmp_path: Path):
    project = _write_project(tmp_path)
    _write_derived_cache(project, "derived_a", "derived_b")

    with pytest.raises(ValueError, match="multiple implementation task candidates"):
        auto_detect_task(project)
