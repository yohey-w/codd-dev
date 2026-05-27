from __future__ import annotations

from codd.coverage_obligation_extractor import (
    ACTION_OUTCOME,
    AGGREGATION_POLICY,
    CRUD_FLOW,
    GLOBAL_ACTION,
    PRESENTATION_LOCALE,
    ROLE_SEQUENCE,
    UNCOVERED,
    extract_coverage_obligations_from_dag,
)
from codd.dag import DAG, Node


def _dag_with_design_attrs(attrs: dict) -> DAG:
    dag = DAG()
    dag.add_node(Node(id="docs/design/app.md", kind="design_doc", path="docs/design/app.md", attributes=attrs))
    return dag


def _ids_by_kind(result, kind: str) -> list[str]:
    return [obligation.obligation_id for obligation in result.obligations_by_kind(kind)]


def test_extracts_existing_declarations_into_obligations():
    dag = _dag_with_design_attrs(
        {
            "user_journeys": [
                {
                    "name": "manage_accounts",
                    "actor": "Admin",
                    "criticality": "critical",
                    "steps": [
                        {"action": "open", "target": "/accounts"},
                        {"action": "expect_text", "value": "Accounts"},
                    ],
                    "expected_outcome_refs": ["lexicon:account_management_visible"],
                }
            ],
            "display_fields": [
                {
                    "name": "billing_date",
                    "actor": "Admin",
                    "field": "billing date",
                    "locale": "en-US",
                    "timezone": "America/New_York",
                    "expected_format": "localized date",
                }
            ],
            "aggregation_policies": [
                {
                    "name": "invoice_total",
                    "actor": "Admin",
                    "metric": "invoice total",
                    "function": "sum",
                    "source_count": "all visible invoices",
                }
            ],
        }
    )
    project_config = {
        "runtime": {
            "global_action_targets": [
                {
                    "name": "session sign out",
                    "actor": "Admin",
                    "action": {"id": "session.sign_out", "outcomes": ["session_absence_after_action"]},
                    "command": "pytest tests/e2e/test_session.py",
                }
            ],
            "action_outcome_targets": [
                {
                    "name": "publish account",
                    "actor": "Admin",
                    "action": {"id": "account.publish", "verb": "publish", "outcomes": ["visible_reflection"]},
                    "command": "pytest tests/e2e/test_publish.py",
                }
            ],
            "crud_flow_targets": [
                {
                    "name": "create account",
                    "actor": "Admin",
                    "command": "pytest tests/e2e/test_account_crud.py",
                    "expect_text": "Created account",
                }
            ],
        }
    }

    result = extract_coverage_obligations_from_dag(dag, project_config=project_config)

    assert _ids_by_kind(result, ROLE_SEQUENCE) == ["obl:role_sequence:admin:manage_accounts"]
    assert _ids_by_kind(result, GLOBAL_ACTION) == ["obl:global_action:admin:session_sign_out"]
    assert _ids_by_kind(result, ACTION_OUTCOME) == ["obl:action_outcome:admin:account_publish"]
    assert _ids_by_kind(result, CRUD_FLOW) == ["obl:crud_flow:admin:create_account"]
    assert _ids_by_kind(result, PRESENTATION_LOCALE) == ["obl:presentation_locale:admin:billing_date"]
    assert _ids_by_kind(result, AGGREGATION_POLICY) == ["obl:aggregation_policy:admin:invoice_total"]
    assert result.obligations_by_kind(ROLE_SEQUENCE)[0].risk_level == "P0"
    assert result.obligations_by_kind(ROLE_SEQUENCE)[0].to_schema_mapping()["waiver_expiry"] is None
    schema_obligation = result.obligations_by_kind(ROLE_SEQUENCE)[0].to_schema_obligation()
    schema_id = (
        schema_obligation.obligation_id
        if hasattr(schema_obligation, "obligation_id")
        else schema_obligation["obligation_id"]
    )
    assert schema_id.endswith("manage_accounts")
    assert result.unsupported_items == []
    assert all("osato" not in obligation.obligation_id for obligation in result.obligations)


def test_actor_without_user_journey_returns_schema_convertible_uncovered_amber_obligation():
    dag = _dag_with_design_attrs({"actors": ["Operator"]})

    result = extract_coverage_obligations_from_dag(dag)

    role_obligations = result.obligations_by_kind(ROLE_SEQUENCE)
    assert len(role_obligations) == 1
    obligation = role_obligations[0]
    assert obligation.obligation_id == "obl:role_sequence:operator:missing_journey"
    assert obligation.coverage_status == UNCOVERED
    assert obligation.source["type"] == "design_doc"
    assert obligation.metadata["severity"] == "amber"
    assert obligation.metadata["inference"] == "actor_without_role_sequence"
    assert obligation.metadata["inferred_source_type"] == "inferred"

    schema_obligation = obligation.to_schema_obligation()
    schema_status = schema_obligation.coverage_status
    schema_status_value = schema_status.value if hasattr(schema_status, "value") else schema_status
    schema_source_type = schema_obligation.source.type
    schema_source_type_value = schema_source_type.value if hasattr(schema_source_type, "value") else schema_source_type
    assert schema_status_value == UNCOVERED
    assert schema_source_type_value != "inferred"


def test_verification_e2e_declaration_becomes_evidence_candidate_not_coverage_proof():
    dag = _dag_with_design_attrs(
        {
            "user_journeys": [
                {
                    "name": "login_flow",
                    "actor": "Admin",
                    "criticality": "high",
                    "steps": [{"action": "expect_url", "value": "/dashboard"}],
                    "expected_outcome_refs": ["lexicon:login_flow"],
                }
            ]
        }
    )
    dag.add_node(
        Node(
            id="verification:e2e:tests/e2e/login_flow.spec.ts",
            kind="verification_test",
            path="tests/e2e/login_flow.spec.ts",
            attributes={
                "kind": "e2e",
                "journey_name": "login_flow",
                "target": "login",
                "expected_outcome": {"source": "tests/e2e/login_flow.spec.ts", "journey_name": "login_flow"},
            },
        )
    )

    result = extract_coverage_obligations_from_dag(dag)

    assert [candidate.ref for candidate in result.evidence_candidates] == ["tests/e2e/login_flow.spec.ts"]
    role = result.obligations_by_kind(ROLE_SEQUENCE)[0]
    assert role.coverage_status == UNCOVERED
    assert role.metadata["evidence_candidates"][0]["kind"] == "e2e"
    assert role.metadata["evidence_candidates"][0]["journey_name"] == "login_flow"


def test_presentation_and_aggregation_absence_is_reported_as_unsupported():
    dag = _dag_with_design_attrs({"user_journeys": []})

    result = extract_coverage_obligations_from_dag(dag)

    unsupported = {item["kind"]: item for item in result.unsupported_items}
    assert PRESENTATION_LOCALE in unsupported
    assert AGGREGATION_POLICY in unsupported
    assert unsupported[PRESENTATION_LOCALE]["status"] == "unsupported"
    assert "presentation_specs" in unsupported[PRESENTATION_LOCALE]["reason"]
    assert unsupported[AGGREGATION_POLICY]["status"] == "unsupported"
