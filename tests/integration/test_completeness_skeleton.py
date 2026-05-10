from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from codd.dag import DAG, Node
from codd.dag.checks.ci_health import CiHealthCheck
from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceCheck
from codd.elicit.engine import ElicitEngine


@dataclass
class ActorLexicon:
    prompt_extension_content: str = "Assess stakeholder role journey coverage."
    coverage_axes: list[dict] | None = None

    def __post_init__(self) -> None:
        if self.coverage_axes is None:
            self.coverage_axes = [{"axis_type": "stakeholder_role"}]


class FakeAi:
    def invoke(self, prompt: str) -> str:
        return json.dumps(
            {
                "metadata": {"stakeholder_roles": ["Operator"]},
                "lexicon_coverage_report": {"stakeholder_role": "covered"},
                "findings": [],
            }
        )


def test_elicit_to_c7_no_journey_declared(tmp_path: Path) -> None:
    (tmp_path / "requirements.md").write_text("Operators can approve queued work.\n", encoding="utf-8")
    design_dir = tmp_path / "docs" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "ops.md").write_text("---\nuser_journeys: []\n---\n# Ops\n", encoding="utf-8")

    elicit = ElicitEngine(ai_command=FakeAi()).run(tmp_path, ActorLexicon())
    dag = DAG()
    dag.add_node(Node(id="docs/design/ops.md", kind="design_doc", attributes={"actors": ["Operator"]}))
    c7 = UserJourneyCoherenceCheck().run(dag, tmp_path, {})

    assert [finding.kind for finding in elicit.findings] == ["missing_journey_for_actor"]
    assert c7.status == "warn"
    assert c7.severity == "amber"


def test_c8_on_skeleton_without_workflow(tmp_path: Path) -> None:
    (tmp_path / "deploy.yaml").write_text("post_deploy: pytest tests/e2e\n", encoding="utf-8")

    result = CiHealthCheck().run(project_root=tmp_path, settings={"ci": {"provider": "github_actions"}})

    assert result.status == "fail"
    assert result.severity == "red"
    assert result.findings[0].violation_type == "ci_workflow_missing"
