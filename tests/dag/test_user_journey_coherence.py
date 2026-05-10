from __future__ import annotations

from pathlib import Path

from codd.dag import DAG, Edge, Node
from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceCheck


def _run(dag: DAG, tmp_path: Path):
    return UserJourneyCoherenceCheck().run(dag, tmp_path, {})


def test_c7_amber_when_actor_present_no_journey(tmp_path: Path) -> None:
    dag = DAG()
    dag.add_node(Node(id="docs/design/ops.md", kind="design_doc", attributes={"actors": ["Operator"]}))

    result = _run(dag, tmp_path)

    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.violations[0]["type"] == "actors_without_journeys"


def test_c7_skip_when_no_actors_no_journeys(tmp_path: Path) -> None:
    dag = DAG()
    dag.add_node(Node(id="docs/design/system.md", kind="design_doc", attributes={}))

    result = _run(dag, tmp_path)

    assert result.status == "pass"
    assert result.passed is True
    assert result.violations == []
    assert "SKIP" in result.message


def test_c7_pass_when_actor_and_journey_both_declared(tmp_path: Path) -> None:
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/ops.md",
            kind="design_doc",
            attributes={
                "actors": ["Operator"],
                "user_journeys": [
                    {
                        "name": "operate_console",
                        "actors": ["Operator"],
                        "steps": [{"action": "expect_status", "value": "ready"}],
                        "expected_outcome_refs": ["lexicon:operate_console"],
                    }
                ],
            },
        )
    )
    dag.add_node(Node(id="lexicon:operate_console", kind="expected", attributes={"path": "tests/e2e/ops.spec.ts"}))
    dag.add_node(
        Node(
            id="plan#operate-console",
            kind="plan_task",
            attributes={"expected_outputs": ["lexicon:operate_console"]},
        )
    )
    dag.add_edge(Edge("plan#operate-console", "lexicon:operate_console", "produces"))
    dag.add_node(
        Node(
            id="verification:e2e:tests/e2e/ops.spec.ts",
            kind="verification_test",
            path="tests/e2e/ops.spec.ts",
            attributes={
                "kind": "e2e",
                "expected_outcome": {"source": "tests/e2e/ops.spec.ts"},
                "in_deploy_flow": True,
            },
        )
    )

    result = _run(dag, tmp_path)

    assert result.status == "pass"
    assert result.passed is True
    assert result.violations == []
