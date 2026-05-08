from __future__ import annotations

import json
from pathlib import Path
import shutil

from click.testing import CliRunner
import yaml

from codd.cli import main
from codd.init import llm_lexicon_suggester as suggester
from codd.init.llm_lexicon_suggester import (
    LlmLexiconRecommendation,
    LlmLexiconResult,
    llm_recommend_lexicons,
)


REPO_ROOT = Path(__file__).parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "sample_react_fastapi_prisma"


class FakeAiCommand:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[str] = []

    def invoke(self, prompt: str, model: str | None = None) -> str:
        self.calls.append(prompt)
        return self.output


def test_llm_enhanced_returns_recommendations_with_confidence(tmp_path: Path) -> None:
    lexicon_id = _first_available_lexicon_id()
    _write_requirements_project(tmp_path)
    fake = FakeAiCommand(
        json.dumps(
            {
                "detected_domain": "managed workflow",
                "detected_compliance": ["policy review"],
                "detected_tech_stack": ["python"],
                "detected_integrations": ["external identity"],
                "recommendations": [
                    {
                        "lexicon_id": lexicon_id,
                        "confidence": "high",
                        "reason": "Project context matches the listed coverage scope.",
                    }
                ],
            }
        )
    )

    result = llm_recommend_lexicons(tmp_path, ai_command=fake)

    assert result.detected_domain == "managed workflow"
    assert result.detected_compliance == ["policy review"]
    assert result.detected_tech_stack == ["python"]
    assert result.detected_integrations == ["external identity"]
    assert result.recommendations == [
        LlmLexiconRecommendation(
            lexicon_id=lexicon_id,
            confidence="high",
            reason="Project context matches the listed coverage scope.",
        )
    ]
    assert "available_lexicons" in fake.calls[0]
    assert "project_context" in fake.calls[0]


def test_auto_approve_skips_hitl(monkeypatch, tmp_path: Path) -> None:
    ids = _first_available_lexicon_ids(3)
    project = tmp_path / "sample"
    shutil.copytree(FIXTURE, project)

    def fake_recommend(project_root: Path):
        return LlmLexiconResult(
            detected_domain="managed workflow",
            detected_compliance=[],
            detected_tech_stack=["python"],
            detected_integrations=[],
            recommendations=[
                LlmLexiconRecommendation(ids[0], "high", "primary match"),
                LlmLexiconRecommendation(ids[1], "medium", "secondary match"),
                LlmLexiconRecommendation(ids[2], "low", "later review"),
            ],
        )

    monkeypatch.setattr(suggester, "llm_recommend_lexicons", fake_recommend)

    result = CliRunner().invoke(
        main,
        [
            "init",
            "--project-name",
            "Sample",
            "--language",
            "python",
            "--dest",
            str(project),
            "--llm-enhanced",
            "--auto-approve",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Apply all recommended?" not in result.output
    data = yaml.safe_load((project / "project_lexicon.yaml").read_text(encoding="utf-8"))
    assert ids[0] in data["extends"]
    assert ids[1] in data["extends"]
    assert ids[2] not in data["extends"]


def test_no_requirements_md_falls_back_to_regex(tmp_path: Path) -> None:
    project = tmp_path / "sample"
    shutil.copytree(FIXTURE, project)

    result = CliRunner().invoke(
        main,
        [
            "init",
            "--project-name",
            "Sample",
            "--language",
            "python",
            "--dest",
            str(project),
            "--llm-enhanced",
        ],
        input="\n",
    )

    assert result.exit_code == 0, result.output
    assert "falling back to stack-based suggestions" in result.output
    assert "Detected signals: package.json, requirements.txt" in result.output
    data = yaml.safe_load((project / "project_lexicon.yaml").read_text(encoding="utf-8"))
    assert data["extends"]


def test_llm_output_json_parse_error_falls_back_gracefully(tmp_path: Path) -> None:
    _write_requirements_project(tmp_path)

    result = llm_recommend_lexicons(tmp_path, ai_command=FakeAiCommand("not json"))

    assert result == LlmLexiconResult(
        detected_domain="",
        detected_compliance=[],
        detected_tech_stack=[],
        detected_integrations=[],
        recommendations=[],
    )


def _write_requirements_project(project: Path) -> None:
    requirements = project / "docs" / "requirements"
    requirements.mkdir(parents=True)
    (requirements / "requirements.md").write_text(
        "# Requirements\n\nThe system manages reviewed work items and external sign-in.\n",
        encoding="utf-8",
    )
    design = project / "docs" / "design"
    design.mkdir(parents=True)
    (design / "overview.md").write_text("# Design\n\nUse a service API and queue workers.\n", encoding="utf-8")
    (project / "pyproject.toml").write_text("[project]\ndependencies = [\"click\"]\n", encoding="utf-8")


def _first_available_lexicon_id() -> str:
    return _first_available_lexicon_ids(1)[0]


def _first_available_lexicon_ids(count: int) -> list[str]:
    ids = [
        manifest.parent.name
        for manifest in sorted((REPO_ROOT / "codd_plugins" / "lexicons").glob("*/manifest.yaml"))
    ]
    assert len(ids) >= count
    return ids[:count]
