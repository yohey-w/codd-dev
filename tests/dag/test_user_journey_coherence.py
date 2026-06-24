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


def test_c7_reads_journey_from_frontmatter_codd(tmp_path: Path) -> None:
    # A journey authored at the canonical frontmatter.codd position must be
    # detected (it has no plan task / e2e test → violations). Before the central
    # metadata helper, _journey_entries read only attributes["user_journeys"], so
    # a codd-nested journey was invisible and C7 PASSED (dormant / false-green).
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/ops.md",
            kind="design_doc",
            attributes={
                "user_journeys": [],
                "frontmatter": {
                    "codd": {
                        "user_journeys": [
                            {
                                "name": "codd_nested_journey",
                                "criticality": "critical",
                                "steps": [{"action": "click"}],
                                "required_capabilities": [],
                                "expected_outcome_refs": [],
                            }
                        ]
                    }
                },
            },
        )
    )

    result = _run(dag, tmp_path)

    journeys = {report["user_journey"] for report in result.journey_reports}
    assert "codd_nested_journey" in journeys
    types = {violation["type"] for violation in result.violations}
    assert "no_plan_task_for_journey" in types


def test_c7_top_level_only_journey_still_detected(tmp_path: Path) -> None:
    # Regression: a top-level-only declaration (extractor-lifted) keeps working.
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/ops.md",
            kind="design_doc",
            attributes={
                "user_journeys": [
                    {
                        "name": "top_level_journey",
                        "criticality": "critical",
                        "steps": [{"action": "click"}],
                        "required_capabilities": [],
                        "expected_outcome_refs": [],
                    }
                ]
            },
        )
    )

    result = _run(dag, tmp_path)

    journeys = {report["user_journey"] for report in result.journey_reports}
    assert "top_level_journey" in journeys


def test_c7_does_not_double_count_lifted_journey(tmp_path: Path) -> None:
    # The extractor lifts a top-level frontmatter journey into BOTH
    # attributes["user_journeys"] AND keeps the raw copy at
    # frontmatter["user_journeys"]. The same declaration must be reported once,
    # not twice (no doubled journey_reports / violations).
    journey = {
        "name": "dedup_journey",
        "criticality": "critical",
        "steps": [{"action": "click"}],
        "required_capabilities": [],
        "expected_outcome_refs": [],
    }
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/ops.md",
            kind="design_doc",
            attributes={
                "user_journeys": [journey],
                "frontmatter": {"user_journeys": [journey]},
            },
        )
    )

    result = _run(dag, tmp_path)

    matching = [r for r in result.journey_reports if r["user_journey"] == "dedup_journey"]
    assert len(matching) == 1


def test_c7_unions_top_level_and_codd_journeys(tmp_path: Path) -> None:
    # A top-level journey PLUS a distinct frontmatter.codd journey are both seen.
    top = {
        "name": "top_journey",
        "criticality": "critical",
        "steps": [{"action": "click"}],
        "required_capabilities": [],
        "expected_outcome_refs": [],
    }
    nested = {
        "name": "nested_journey",
        "criticality": "critical",
        "steps": [{"action": "click"}],
        "required_capabilities": [],
        "expected_outcome_refs": [],
    }
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/ops.md",
            kind="design_doc",
            attributes={
                "user_journeys": [top],
                "frontmatter": {
                    "user_journeys": [top],  # raw lifted duplicate
                    "codd": {"user_journeys": [nested]},
                },
            },
        )
    )

    result = _run(dag, tmp_path)

    journeys = {report["user_journey"] for report in result.journey_reports}
    assert journeys == {"top_journey", "nested_journey"}
