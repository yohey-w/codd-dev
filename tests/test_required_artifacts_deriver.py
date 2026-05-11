"""Tests for required design artifact derivation."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from click.testing import CliRunner
import pytest
import yaml

from codd.cli import main
from codd.lexicon import (
    AskItem,
    AskOption,
    LexiconError,
    ProjectLexicon,
    load_lexicon,
    validate_lexicon,
)
import codd.required_artifacts_deriver as deriver_module
from codd.required_artifacts_deriver import DEFAULTS_DIR, RequiredArtifactsDeriver


AI_RESPONSE = {
    "required_artifacts": [
        {
            "id": "design:requirements",
            "title": "Requirements Definition",
            "depends_on": [],
            "scope": "Requirements and acceptance criteria",
            "rationale": "All implementation work needs a requirements baseline.",
            "source": "ai_derived",
        },
        {
            "id": "design:screen_flow_design",
            "title": "Screen Flow Design",
            "depends_on": ["design:ux_design"],
            "scope": "Login, dashboard, and detail navigation transitions",
            "rationale": "Requirements describe multi-screen navigation.",
            "source": "ai_derived",
            "derived_from": ["q_auth_method"],
        },
    ]
}


def _write_config(project: Path, extra: str = "") -> None:
    codd_dir = project / ".codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(extra or "{}\n", encoding="utf-8")


def _write_requirements(project: Path, text: str = "Build a product.") -> Path:
    req_dir = project / "docs" / "requirements"
    req_dir.mkdir(parents=True)
    path = req_dir / "requirements.md"
    path.write_text(text, encoding="utf-8")
    return path


def _write_lexicon(project: Path, data: dict) -> None:
    (project / "project_lexicon.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _valid_lexicon() -> dict:
    return {
        "node_vocabulary": [],
        "naming_conventions": [],
        "design_principles": [],
        "coverage_decisions": [
            {
                "id": "q_auth_method",
                "question": "Which authentication method is required?",
                "status": "RECOMMENDED_PROCEEDING",
                "recommended_id": "oauth_oidc",
                "proceeded_with": "oauth_oidc",
                "options": [
                    {
                        "id": "oauth_oidc",
                        "label": "OAuth/OIDC",
                        "recommended": True,
                    }
                ],
            }
        ],
    }


def _ask_item() -> AskItem:
    return AskItem(
        id="q_auth_method",
        question="Which authentication method is required?",
        status="RECOMMENDED_PROCEEDING",
        recommended_id="oauth_oidc",
        proceeded_with="oauth_oidc",
        options=[
            AskOption(
                id="oauth_oidc",
                label="OAuth/OIDC",
                recommended=True,
            )
        ],
    )


def _mock_ai(monkeypatch, response: dict | str = AI_RESPONSE):
    calls: list[dict] = []
    stdout = response if isinstance(response, str) else json.dumps(response)

    def fake_run(command, *, input, capture_output, text, timeout, check, **kwargs):
        calls.append(
            {
                "command": command,
                "input": input,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
                "check": check,
            }
        )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(deriver_module.subprocess, "run", fake_run)
    return calls


def test_initializes_with_detected_web_project_type(tmp_path):
    _write_config(tmp_path)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    deriver = RequiredArtifactsDeriver(tmp_path)

    assert deriver.project_type == "web"


def test_initializes_with_detected_cli_project_type(tmp_path):
    _write_config(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='tool'\n", encoding="utf-8")

    deriver = RequiredArtifactsDeriver(tmp_path)

    assert deriver.project_type == "cli"


def test_load_defaults_reads_web_yaml(tmp_path):
    _write_config(tmp_path)

    defaults = RequiredArtifactsDeriver(tmp_path)._load_defaults("web")

    assert any(item["id"] == "design:requirements" for item in defaults)
    assert any(item["id"] == "design:screen_flow_design" for item in defaults)


def test_load_defaults_reads_cli_yaml(tmp_path):
    _write_config(tmp_path)

    defaults = RequiredArtifactsDeriver(tmp_path)._load_defaults("cli")

    assert any(item["id"] == "design:command_interface_design" for item in defaults)


def test_codd_yaml_project_type_override_wins(tmp_path):
    _write_config(tmp_path, "project:\n  type: iot\n")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    deriver = RequiredArtifactsDeriver(tmp_path)

    assert deriver.project_type == "iot"


def test_build_ai_prompt_includes_project_type_hint(tmp_path):
    _write_config(tmp_path)
    deriver = RequiredArtifactsDeriver(tmp_path)

    prompt = deriver._build_ai_prompt("Requirements", "mobile", "", [])

    assert "Project type: mobile" in prompt


def test_build_ai_prompt_includes_coverage_decisions(tmp_path):
    _write_config(tmp_path)
    deriver = RequiredArtifactsDeriver(tmp_path)

    prompt = deriver._build_ai_prompt(
        "Requirements",
        "web",
        deriver._summarize_decisions([_ask_item()]),
        [],
    )

    assert "q_auth_method" in prompt
    assert "oauth_oidc" in prompt


def test_build_ai_prompt_handles_missing_coverage_decisions(tmp_path):
    _write_config(tmp_path)
    deriver = RequiredArtifactsDeriver(tmp_path)

    prompt = deriver._build_ai_prompt("Requirements", "web", deriver._summarize_decisions([]), [])

    assert "User decisions from requirement completeness audit:" in prompt
    assert "(none)" in prompt


def test_call_ai_invokes_subprocess_run(monkeypatch, tmp_path):
    _write_config(tmp_path, "required_artifacts:\n  ai_timeout_seconds: 7\n")
    calls = _mock_ai(monkeypatch)

    response = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai --print")._call_ai("prompt")

    assert response == json.dumps(AI_RESPONSE)
    assert calls[0]["command"] == ["mock-ai", "--print"]
    assert calls[0]["input"] == "prompt"
    assert calls[0]["timeout"] == 7


def test_parse_ai_response_returns_artifact_list(tmp_path):
    _write_config(tmp_path)

    artifacts = RequiredArtifactsDeriver(tmp_path)._parse_ai_response(json.dumps(AI_RESPONSE))

    assert artifacts[0]["id"] == "design:requirements"
    assert artifacts[1]["derived_from"] == ["q_auth_method"]


def test_parse_ai_response_rejects_invalid_json(tmp_path):
    _write_config(tmp_path)

    with pytest.raises(ValueError, match="valid JSON"):
        RequiredArtifactsDeriver(tmp_path)._parse_ai_response("not json")


def test_derive_full_flow_uses_requirements_defaults_and_ai(monkeypatch, tmp_path):
    _write_config(tmp_path, "project:\n  type: web\n")
    _write_requirements(tmp_path, "Users log in and navigate from dashboard to detail pages.")
    calls = _mock_ai(monkeypatch)

    artifacts = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive([], [_ask_item()])

    assert artifacts == AI_RESPONSE["required_artifacts"]
    prompt = calls[0]["input"]
    assert "Users log in" in prompt
    assert "Default candidate artifacts" in prompt
    assert "q_auth_method" in prompt


def test_derive_works_without_coverage_decisions(monkeypatch, tmp_path):
    _write_config(tmp_path)
    _write_requirements(tmp_path)
    _mock_ai(monkeypatch)

    artifacts = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive([], None)

    assert artifacts[0]["source"] == "ai_derived"


def test_project_lexicon_required_artifacts_field_round_trips(tmp_path):
    data = _valid_lexicon()
    lexicon = ProjectLexicon(data)
    lexicon.set_required_artifacts(AI_RESPONSE["required_artifacts"])
    output = lexicon.as_dict()

    validate_lexicon(output)
    _write_lexicon(tmp_path, output)

    loaded = load_lexicon(tmp_path)
    assert loaded.required_artifacts[1]["id"] == "design:screen_flow_design"
    assert loaded.required_artifacts[1]["source"] == "ai_derived"


def test_project_lexicon_rejects_required_artifact_without_valid_source():
    data = _valid_lexicon()
    data["required_artifacts"] = [
        {
            "id": "design:x",
            "title": "X",
            "depends_on": [],
            "scope": "Scope",
            "source": "unknown",
        }
    ]

    with pytest.raises(LexiconError, match=r"source must be one of"):
        validate_lexicon(data)


def test_cli_plan_derive_writes_lexicon(monkeypatch, tmp_path):
    _write_config(tmp_path, "ai_command: mock-ai --print\n")
    _write_requirements(tmp_path, "Users log in and navigate to dashboard.")
    _write_lexicon(tmp_path, _valid_lexicon())
    _mock_ai(monkeypatch)

    result = CliRunner().invoke(main, ["plan", "--path", str(tmp_path), "--derive"])

    assert result.exit_code == 0
    assert "Derived 2 required artifact(s)." in result.output
    assert load_lexicon(tmp_path).required_artifacts == AI_RESPONSE["required_artifacts"]


def test_generality_gate_keeps_artifact_names_out_of_core_python():
    source = Path("codd/required_artifacts_deriver.py").read_text(encoding="utf-8")

    assert "design:screen_flow_design" not in source
    assert "Command Interface Design" not in source
    assert "OTA Update Design" not in source


def test_project_type_defaults_exist_for_all_required_artifact_types():
    assert sorted(path.stem for path in DEFAULTS_DIR.glob("*.yaml")) == [
        "cli",
        "iot",
        "mobile",
        "web",
    ]


def test_osato_lms_sample_can_require_screen_flow(monkeypatch, tmp_path):
    _write_config(tmp_path, "project:\n  type: web\n")
    _write_requirements(
        tmp_path,
        "Osato LMS needs login -> dashboard -> lesson detail navigation with guarded transitions.",
    )
    _mock_ai(monkeypatch)

    artifacts = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive([], [])

    assert any(artifact["id"] == "design:screen_flow_design" for artifact in artifacts)
