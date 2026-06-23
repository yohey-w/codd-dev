"""Tests for the resource_flow_coherence DAG check (consumed-but-never-produced)."""

from __future__ import annotations

from codd.dag import DAG, Node
from codd.dag.checks.resource_flow_coherence import ResourceFlowCoherenceCheck


def _dag(*nodes: Node) -> DAG:
    dag = DAG()
    for node in nodes:
        dag.add_node(node)
    return dag


def _design_doc(node_id: str = "docs/design/integration.md", **attributes) -> Node:
    return Node(id=node_id, kind="design_doc", attributes=attributes)


def _run(dag: DAG):
    return ResourceFlowCoherenceCheck(dag).run()


CRITICAL_JOURNEY = {
    "name": "line_individual_nudge_to_inactive_learners",
    "criticality": "critical",
    "steps": [{"action": "send_individual_line_nudge"}],
    "required_capabilities": ["line_individual_nudge", "lstep_tag_reflection"],
    "expected_outcome_refs": [],
}


# Fixture 1 — existing projects with no contracts keep passing.
def test_no_contracts_skips():
    dag = _dag(_design_doc(user_journeys=[CRITICAL_JOURNEY]))
    result = _run(dag)
    assert result.skipped is True
    assert result.passed is True
    assert result.status == "skip"


# Fixture 2 — required consumer in a critical journey with no producer → RED.
def test_required_consumer_without_producer_is_red():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                    "produces": [],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is False
    assert result.severity == "red"
    assert any(
        v["type"] == "dangling_required_consumer"
        and v["resource"] == "data:users.lstep_friend_id"
        and v["consumer_capability"] == "line_individual_nudge"
        for v in result.violations
    )


# Fixture 3 — add a producer obligation → GREEN.
def test_required_consumer_with_producer_passes():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
                {
                    "capability": "bind_line_friend_to_user",
                    "produces": [{"resource": "data:users.lstep_friend_id"}],
                },
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert result.violations == []


# Fixture 4a — optional consumer (required: false / on_missing: skip) → not gated.
def test_optional_consumer_passes():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": False,
                            "on_missing": "skip",
                        }
                    ],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert result.violations == []


# Fixture 4b — externally provided resource satisfies the consumer.
def test_external_provider_satisfies_consumer():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                }
            ],
            resource_contracts=[
                {
                    "resource": "data:users.lstep_friend_id",
                    "externally_provided_by": [{"provider": "lstep_friend_webhook"}],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True


# A required read whose capability is not on a critical journey is advisory, not red.
def test_required_read_off_critical_journey_is_warning():
    dag = _dag(
        _design_doc(
            user_journeys=[
                {
                    "name": "low_priority",
                    "criticality": "low",
                    "required_capabilities": ["line_individual_nudge"],
                    "steps": [],
                    "expected_outcome_refs": [],
                }
            ],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert any(w["type"] == "unscoped_resource_consumer" for w in result.warnings)


# Regression — the real osato false-green: per-person LINE nudge + tag reflection
# both read lstep_friend_id, the webhook never writes it, no producer declared → RED.
def test_osato_lstep_friend_id_regression():
    dag = _dag(
        _design_doc(
            node_id="docs/design/integration_design.md",
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                            "reason": "LINE個別送信の宛先解決に必要",
                        }
                    ],
                },
                {
                    "capability": "lstep_tag_reflection",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
            ],
        )
    )
    result = _run(dag)
    assert result.passed is False
    assert result.severity == "red"
    assert {v["resource"] for v in result.violations} == {"data:users.lstep_friend_id"}
    assert {v["consumer_capability"] for v in result.violations} == {
        "line_individual_nudge",
        "lstep_tag_reflection",
    }
