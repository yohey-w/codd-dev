from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from click.testing import CliRunner
import pytest
import yaml

from codd.cli import main
from codd.elicit.engine import ElicitEngine
from codd.elicit.finding import Finding
from codd.elicit.persistence import save_pending


@dataclass
class LexiconStub:
    lexicon_name: str = "sample"
    prompt_extension_content: str = "BASE {{requirements_content}}\nEXT {{project_lexicon}}\n{{existing_axes}}"
    recommended_kinds: list[str] | None = None


class FakeAiCommand:
    def __init__(self, output: str):
        self.output = output
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.output


class FakeSubprocessAiCommand:
    prompts: list[str] = []

    def __init__(self, command=None, project_root=None):
        self.command = command
        self.project_root = project_root

    def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps([_finding_payload("F-1")])


def _finding_payload(finding_id: str = "F-1") -> dict:
    return {
        "id": finding_id,
        "kind": "gap",
        "severity": "medium",
        "name": "Missing decision",
        "question": "What should happen?",
        "details": {"evidence": "not stated"},
        "related_requirement_ids": ["REQ-1"],
        "rationale": "The material leaves a decision open.",
    }


def _write_project_docs(project_root: Path) -> None:
    (project_root / "requirements.md").write_text("# Requirements\nREQ-1\n", encoding="utf-8")
    (project_root / "docs" / "design").mkdir(parents=True)
    (project_root / "docs" / "design" / "feature.md").write_text("# Design\nDecision\n", encoding="utf-8")
    (project_root / "project_lexicon.yaml").write_text("terms:\n  - sample\n", encoding="utf-8")
    (project_root / ".codd").mkdir()
    (project_root / ".codd" / "codd.yaml").write_text(
        yaml.safe_dump({"coverage_axes": ["first"]}),
        encoding="utf-8",
    )


def test_engine_initializes_with_defaults() -> None:
    engine = ElicitEngine()

    assert engine.template_path.name == "elicit_prompt_L0.md"


def test_build_prompt_loads_project_material(tmp_path: Path) -> None:
    _write_project_docs(tmp_path)

    prompt = ElicitEngine().build_prompt(tmp_path)

    assert "REQ-1" in prompt
    assert "docs/design/feature.md" in prompt
    assert "terms:" in prompt
    assert "coverage_axes" in prompt


def test_build_prompt_handles_missing_project_material(tmp_path: Path) -> None:
    prompt = ElicitEngine().build_prompt(tmp_path)

    assert "(none provided)" in prompt
    assert "{{requirements_content}}" not in prompt


def test_build_prompt_uses_lexicon_extension_content(tmp_path: Path) -> None:
    _write_project_docs(tmp_path)

    prompt = ElicitEngine().build_prompt(
        tmp_path,
        lexicon_config=LexiconStub(recommended_kinds=["first_kind"]),
    )

    assert prompt.startswith("BASE")
    assert "recommended_kinds" in prompt
    assert "first_kind" in prompt


def test_build_prompt_accepts_mapping_lexicon_config(tmp_path: Path) -> None:
    prompt = ElicitEngine().build_prompt(
        tmp_path,
        lexicon_config={
            "lexicon_name": "sample",
            "prompt_extension_content": "LEX {{requirements_content}}",
        },
    )

    assert prompt.startswith("LEX")


def test_deserialize_accepts_json_array() -> None:
    findings = ElicitEngine().deserialize(json.dumps([_finding_payload("F-1")]))

    assert findings == [Finding.from_dict(_finding_payload("F-1"))]


def test_deserialize_accepts_fenced_json_array() -> None:
    raw = "```json\n" + json.dumps([_finding_payload("F-1")]) + "\n```"

    assert ElicitEngine().deserialize(raw)[0].id == "F-1"


def test_deserialize_extracts_array_from_surrounding_text() -> None:
    raw = "Result:\n" + json.dumps([_finding_payload("F-1")]) + "\nDone"

    assert ElicitEngine().deserialize(raw)[0].id == "F-1"


def test_deserialize_rejects_non_array() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        ElicitEngine().deserialize(json.dumps({"id": "F-1"}))


def test_deserialize_rejects_missing_array() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        ElicitEngine().deserialize("no structured output")


def test_run_invokes_object_ai_and_returns_findings(tmp_path: Path) -> None:
    _write_project_docs(tmp_path)
    ai = FakeAiCommand(json.dumps([_finding_payload("F-1")]))

    findings = ElicitEngine(ai_command=ai).run(tmp_path)

    assert findings == [Finding.from_dict(_finding_payload("F-1"))]
    assert "REQ-1" in ai.prompts[0]


def test_run_accepts_callable_ai(tmp_path: Path) -> None:
    _write_project_docs(tmp_path)

    findings = ElicitEngine(ai_command=lambda prompt: json.dumps([_finding_payload("F-1")])).run(tmp_path)

    assert findings[0].id == "F-1"


def test_run_filters_pending_findings(tmp_path: Path) -> None:
    _write_project_docs(tmp_path)
    save_pending(tmp_path, [Finding.from_dict(_finding_payload("F-1"))])
    ai = FakeAiCommand(json.dumps([_finding_payload("F-1"), _finding_payload("F-2")]))

    findings = ElicitEngine(ai_command=ai).run(tmp_path)

    assert [finding.id for finding in findings] == ["F-2"]


def test_run_filters_ignored_findings(tmp_path: Path) -> None:
    _write_project_docs(tmp_path)
    ignored_path = tmp_path / ".codd" / "elicit" / "ignored_findings.yaml"
    ignored_path.parent.mkdir(parents=True, exist_ok=True)
    ignored_path.write_text(yaml.safe_dump({"ignored": [{"id": "F-1"}]}), encoding="utf-8")
    ai = FakeAiCommand(json.dumps([_finding_payload("F-1"), _finding_payload("F-2")]))

    findings = ElicitEngine(ai_command=ai).run(tmp_path)

    assert [finding.id for finding in findings] == ["F-2"]


def test_context_size_is_limited(tmp_path: Path) -> None:
    (tmp_path / "requirements.md").write_text("A" * 200, encoding="utf-8")

    prompt = ElicitEngine(max_context_chars=40).build_prompt(tmp_path)

    assert "A" * 80 not in prompt


def test_cli_elicit_writes_markdown_output(tmp_path: Path, monkeypatch) -> None:
    _write_project_docs(tmp_path)
    FakeSubprocessAiCommand.prompts = []
    monkeypatch.setattr("codd.elicit.engine.SubprocessAiCommand", FakeSubprocessAiCommand)

    result = CliRunner().invoke(main, ["elicit", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Elicit discovery complete: findings=1" in result.output
    assert "## F-1 - Missing decision" in (tmp_path / "findings.md").read_text(encoding="utf-8")
    assert "REQ-1" in FakeSubprocessAiCommand.prompts[0]


def test_cli_elicit_outputs_json(tmp_path: Path, monkeypatch) -> None:
    _write_project_docs(tmp_path)
    monkeypatch.setattr("codd.elicit.engine.SubprocessAiCommand", FakeSubprocessAiCommand)

    result = CliRunner().invoke(main, ["elicit", "--format", "json", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["id"] == "F-1"
