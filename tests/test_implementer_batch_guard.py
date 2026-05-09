from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import yaml

import codd.implementer as implementer_module
from codd.cli import main


def _write_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "codd").mkdir()
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "typescript"},
                "ai_command": "mock-ai --print",
                "implement": {"default_output_paths": {"docs/design/auth.md": ["src/auth"]}},
                "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/design/"], "config_files": [], "exclude": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    design = project / "docs" / "design" / "auth.md"
    design.parent.mkdir(parents=True)
    design.write_text(
        "---\ncodd:\n  node_id: design:auth\n  type: design\n---\n\n# Auth Design\n",
        encoding="utf-8",
    )
    return project


def test_implement_help_exposes_design_output_api() -> None:
    result = CliRunner().invoke(main, ["implement", "--help"])

    assert result.exit_code == 0
    assert "--design" in result.output
    assert "--output" in result.output
    assert "--depends-on" in result.output
    assert "--wave" not in result.output
    assert "--max-tasks" not in result.output


def test_implement_cli_passes_direct_options(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_implement_tasks(project_root, *, design, output_paths, dependency_design_nodes, ai_command, clean):
        captured.update(
            {
                "project_root": project_root,
                "design": design,
                "output_paths": output_paths,
                "dependency_design_nodes": dependency_design_nodes,
                "ai_command": ai_command,
                "clean": clean,
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
            "--design",
            "docs/design/auth.md",
            "--output",
            "src/auth",
            "--depends-on",
            "docs/design/shared.md",
            "--ai-cmd",
            "custom-ai --print",
            "--clean",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "project_root": project.resolve(),
        "design": "docs/design/auth.md",
        "output_paths": ["src/auth"],
        "dependency_design_nodes": ["docs/design/shared.md"],
        "ai_command": "custom-ai --print",
        "clean": True,
    }
