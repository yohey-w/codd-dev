from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import yaml

from codd.cli import main


class FakeSubprocessAiCommand:
    prompts: list[str] = []

    def __init__(self, command=None, project_root=None):
        self.command = command
        self.project_root = project_root

    def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        finding_id = "F-DISCOVERY"
        if "LEXICON_ONE" in prompt:
            finding_id = "F-ONE"
        elif "LEXICON_TWO" in prompt:
            finding_id = "F-TWO"
        return json.dumps(
            [
                {
                    "id": finding_id,
                    "kind": "gap",
                    "severity": "medium",
                    "details": {"dimension": finding_id.lower()},
                }
            ]
        )


def _write_project(tmp_path: Path, extends: list[str] | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.md").write_text("Requirement text", encoding="utf-8")
    payload = {
        "node_vocabulary": [],
        "naming_conventions": [],
        "design_principles": [],
    }
    if extends is not None:
        payload["extends"] = extends
    (project / "project_lexicon.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
    return project


def _write_lexicon(project: Path, lexicon_id: str, marker: str) -> None:
    root = project / lexicon_id
    root.mkdir()
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "lexicon_name": lexicon_id,
                "prompt_extension": "elicit_extend.md",
                "recommended_kinds": "recommended_kinds.yaml",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "elicit_extend.md").write_text(
        f"{marker} {{{{requirements_content}}}} {{{{project_lexicon}}}}",
        encoding="utf-8",
    )
    (root / "recommended_kinds.yaml").write_text(
        yaml.safe_dump({"recommended_kinds": [f"{lexicon_id}_gap"]}),
        encoding="utf-8",
    )


def test_elicit_no_lexicon_arg_loads_extends_lexicons(tmp_path: Path, monkeypatch) -> None:
    project = _write_project(tmp_path, extends=["lexicon_one", "lexicon_two"])
    _write_lexicon(project, "lexicon_one", "LEXICON_ONE")
    _write_lexicon(project, "lexicon_two", "LEXICON_TWO")
    FakeSubprocessAiCommand.prompts = []
    monkeypatch.setattr("codd.elicit.engine.SubprocessAiCommand", FakeSubprocessAiCommand)

    result = CliRunner().invoke(main, ["elicit", "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert "findings=2" in result.output
    assert len(FakeSubprocessAiCommand.prompts) == 2


def test_elicit_no_extends_falls_back_to_discovery_mode(tmp_path: Path, monkeypatch) -> None:
    project = _write_project(tmp_path)
    FakeSubprocessAiCommand.prompts = []
    monkeypatch.setattr("codd.elicit.engine.SubprocessAiCommand", FakeSubprocessAiCommand)

    result = CliRunner().invoke(main, ["elicit", "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert "findings=1" in result.output
    assert len(FakeSubprocessAiCommand.prompts) == 1


def test_lexicon_csv_loads_multiple(tmp_path: Path, monkeypatch) -> None:
    project = _write_project(tmp_path)
    _write_lexicon(project, "lexicon_one", "LEXICON_ONE")
    _write_lexicon(project, "lexicon_two", "LEXICON_TWO")
    FakeSubprocessAiCommand.prompts = []
    monkeypatch.setattr("codd.elicit.engine.SubprocessAiCommand", FakeSubprocessAiCommand)

    result = CliRunner().invoke(
        main,
        ["elicit", "--path", str(project), "--lexicon", "lexicon_one,lexicon_two"],
    )

    assert result.exit_code == 0, result.output
    assert "findings=2" in result.output
    assert len(FakeSubprocessAiCommand.prompts) == 2


def test_lexicon_arg_overrides_extends(tmp_path: Path, monkeypatch) -> None:
    project = _write_project(tmp_path, extends=["lexicon_one"])
    _write_lexicon(project, "lexicon_one", "LEXICON_ONE")
    _write_lexicon(project, "lexicon_two", "LEXICON_TWO")
    FakeSubprocessAiCommand.prompts = []
    monkeypatch.setattr("codd.elicit.engine.SubprocessAiCommand", FakeSubprocessAiCommand)

    result = CliRunner().invoke(
        main,
        ["elicit", "--path", str(project), "--lexicon", "lexicon_two"],
    )

    assert result.exit_code == 0, result.output
    assert "findings=1" in result.output
    assert len(FakeSubprocessAiCommand.prompts) == 1
    assert "LEXICON_TWO" in FakeSubprocessAiCommand.prompts[0]
