from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import yaml

from codd.elicit.engine import ElicitEngine


@dataclass
class ActorLexicon:
    lexicon_name: str = "actor-process"
    prompt_extension_content: str = "Assess stakeholder roles and user journeys."
    coverage_axes: list[dict] | None = None

    def __post_init__(self) -> None:
        if self.coverage_axes is None:
            self.coverage_axes = [{"axis_type": "stakeholder_role"}]


class FakeAi:
    def __init__(self, payload: dict):
        self.payload = payload

    def invoke(self, prompt: str) -> str:
        return json.dumps(self.payload)


def _write_design(project_root: Path, frontmatter: dict) -> None:
    design_dir = project_root / "docs" / "design"
    design_dir.mkdir(parents=True)
    body = yaml.safe_dump(frontmatter, sort_keys=False)
    (design_dir / "feature.md").write_text(f"---\n{body}---\n# Feature\n", encoding="utf-8")


def _run(project_root: Path, roles: list[str]) -> list:
    payload = {
        "metadata": {"stakeholder_roles": roles},
        "lexicon_coverage_report": {"stakeholder_role": "covered"},
        "findings": [],
    }
    return ElicitEngine(ai_command=FakeAi(payload)).run(project_root, ActorLexicon()).findings


def test_elicit_missing_journey_for_actor_emitted(tmp_path: Path) -> None:
    _write_design(tmp_path, {"user_journeys": []})

    findings = _run(tmp_path, ["Operator"])

    assert [finding.kind for finding in findings] == ["missing_journey_for_actor"]
    assert findings[0].severity == "amber"
    assert findings[0].actor == "Operator"


def test_elicit_no_finding_when_journey_declared(tmp_path: Path) -> None:
    _write_design(tmp_path, {"user_journeys": [{"name": "operate_console", "actors": ["Operator"]}]})

    findings = _run(tmp_path, ["Operator"])

    assert findings == []


def test_elicit_no_finding_when_no_actors(tmp_path: Path) -> None:
    _write_design(tmp_path, {"user_journeys": []})

    findings = _run(tmp_path, [])

    assert findings == []
