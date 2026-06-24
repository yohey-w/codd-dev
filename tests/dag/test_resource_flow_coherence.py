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


def _warnings_of_type(result, warning_type: str) -> list[dict]:
    return [warning for warning in result.warnings if warning["type"] == warning_type]


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
    assert result.warnings == []


# Fixture 1 — green control: produced and consumed resources are not dead.
def test_dead_resource_green_control_produced_and_consumed_has_no_warning():
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
    assert _warnings_of_type(result, "dead_resource") == []


# Fixture 2 — false-green candidate: produced with no consumers is amber only.
def test_dead_resource_false_green_candidate_warns_amber_only():
    dag = _dag(
        _design_doc(
            capability_contracts=[
                {
                    "capability": "build_unused_export",
                    "produces": [{"resource": "data:exports.unused"}],
                }
            ],
        )
    )
    result = _run(dag)
    dead_resource_warnings = _warnings_of_type(result, "dead_resource")
    assert result.passed is True
    assert result.violations == []
    assert len(dead_resource_warnings) == 1
    assert dead_resource_warnings[0]["severity"] == "amber"
    assert dead_resource_warnings[0]["resource"] == "data:exports.unused"


# Fixture 3 — false-red guard: consumed-only and external resources are not dead.
def test_dead_resource_false_red_guard_ignores_consumed_only_and_external():
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
                    "resource": "data:vendor.seed_payload",
                    "externally_provided_by": [{"provider": "vendor_seed_file"}],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is False
    assert any(v["type"] == "dangling_required_consumer" for v in result.violations)
    assert _warnings_of_type(result, "dead_resource") == []


# Fixture 4 — legacy backcompat: no contracts remain a clean skip.
def test_dead_resource_legacy_backcompat_no_contracts_stays_quiet():
    dag = _dag(_design_doc(user_journeys=[CRITICAL_JOURNEY]))
    result = _run(dag)
    assert result.skipped is True
    assert result.passed is True
    assert result.status == "skip"
    assert result.warnings == []


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


# ── malformed_contract (Tier-1 of extractor_silent_noop): declared-but-unusable ──

# candidate: a consume declared without its required `resource` is surfaced, not dropped.
def test_malformed_contract_entry_is_amber():
    dag = _dag(
        _design_doc(
            capability_contracts=[
                {"capability": "line_individual_nudge", "consumes": [{"required": True}]},
            ],
        )
    )
    result = _run(dag)
    assert any(w["type"] == "malformed_contract" for w in result.warnings)


# guard: a well-formed contract emits no malformed_contract warning.
def test_wellformed_contract_no_malformed_warning():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "bind_line_friend_to_user",
                    "produces": [{"resource": "data:users.lstep_friend_id"}],
                },
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
            ],
        )
    )
    result = _run(dag)
    assert not any(w["type"] == "malformed_contract" for w in result.warnings)


# legacy: no contracts at all → skip, no malformed_contract warning.
def test_malformed_contract_skip_no_contracts():
    dag = _dag(_design_doc(user_journeys=[CRITICAL_JOURNEY]))
    result = _run(dag)
    assert result.skipped is True
    assert not any(w["type"] == "malformed_contract" for w in result.warnings)


# ── diagnostic_incompleteness: every red carries an actionable remediation ──
def test_dangling_consumer_violation_has_remediation():
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
        )
    )
    result = _run(dag)
    assert result.violations
    assert all(v.get("remediation") for v in result.violations)


# every amber finding is self-repairable too (dead_resource shown; malformed/unscoped likewise).
def test_dead_resource_warning_has_remediation():
    dag = _dag(
        _design_doc(
            capability_contracts=[
                {"capability": "build_report", "produces": [{"resource": "data:report.cache"}]},
            ],
        )
    )
    result = _run(dag)
    dead = [w for w in result.warnings if w["type"] == "dead_resource"]
    assert dead and all(w.get("remediation") for w in dead)


# transparency: a PASS reports how many resource uses it actually checked.
def test_pass_message_reports_checked_count():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "bind_line_friend_to_user",
                    "produces": [{"resource": "data:users.lstep_friend_id"}],
                },
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
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert "checked" in result.message
