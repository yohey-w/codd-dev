from __future__ import annotations

from codd.coverage_e2e_selection import (
    GeneratedE2ECandidateRecord,
    candidate_selection_payload,
    generate_e2e_candidates,
    select_e2e_suite,
)
from codd.coverage_obligations import coverage_obligation_from_mapping


def _obligation_payload(**overrides):
    payload = {
        "obligation_id": "obl:role_sequence:admin:dashboard",
        "source": {
            "type": "design_doc",
            "ref": "docs/design/app.md#user_journeys[dashboard]",
        },
        "kind": "role_sequence",
        "actor": "admin",
        "goal": "Reach the dashboard through the browser flow.",
        "preconditions": ["admin is authenticated"],
        "expected_outcomes": ["dashboard is visible"],
        "side_effects": [],
        "risk_level": "P1",
        "coverage_status": "uncovered",
        "covered_by": [],
        "waiver_reason": None,
        "waiver_expiry": None,
    }
    payload.update(overrides)
    return payload


def test_normal_selection_generates_and_selects_uncovered_candidate():
    obligations = [
        _obligation_payload(
            obligation_id="obl:role_sequence:admin:dashboard",
            metadata={"journey_name": "dashboard"},
            pairwise_parameters={"breakpoint": ["desktop", "mobile"]},
        )
    ]

    payload = candidate_selection_payload(obligations, today="2026-05-27")

    assert [item["candidate_id"] for item in payload["generated_e2e_candidates"]] == [
        "candidate:e2e:obl_role_sequence_admin_dashboard"
    ]
    candidate = payload["generated_e2e_candidates"][0]
    assert candidate["obligation_ids"] == ["obl:role_sequence:admin:dashboard"]
    assert candidate["actor"] == "admin"
    assert candidate["journey_or_flow"] == "dashboard"
    assert candidate["risk_level"] == "P1"
    assert candidate["recommended_test_type"] == "browser_role_sequence"
    assert candidate["status"] == "candidate"
    assert candidate["selected_reason"] is None
    assert candidate["future_marker"]["pairwise"]["status"] == "future_todo"
    assert candidate["future_marker"]["pairwise"]["declared"] is True
    assert candidate["future_marker"]["t_way"]["status"] == "future_todo"
    assert payload["selected_e2e_suite"][0]["candidate_id"] == candidate["candidate_id"]
    assert payload["selected_e2e_suite"][0]["selected_reason"].startswith("set_cover_iteration=1")
    assert payload["trace_matrix"][0]["covered_by"] == []


def test_accepts_canonical_coverage_obligation_objects():
    obligation = coverage_obligation_from_mapping(
        _obligation_payload(
            obligation_id="obl:crud_flow:admin:create_record",
            kind="crud_flow",
            goal="Create a record and see it in the UI.",
            risk_level="P0",
        ),
        today="2026-05-27",
    )

    candidates = generate_e2e_candidates([obligation], today="2026-05-27")

    assert [candidate.candidate_id for candidate in candidates] == [
        "candidate:e2e:obl_crud_flow_admin_create_record"
    ]
    assert candidates[0].recommended_test_type == "browser_crud_flow"


def test_high_risk_priority_orders_selection_before_lower_risk():
    obligations = [
        _obligation_payload(
            obligation_id="obl:action_outcome:admin:low",
            kind="action_outcome",
            goal="Perform a low-risk action.",
            risk_level="P3",
        ),
        _obligation_payload(
            obligation_id="obl:action_outcome:admin:high",
            kind="action_outcome",
            goal="Perform a high-risk action.",
            risk_level="P0",
        ),
    ]
    candidates = generate_e2e_candidates(reversed(obligations), today="2026-05-27")

    selected = select_e2e_suite(candidates, obligations, today="2026-05-27")

    assert [item.candidate_id for item in selected] == [
        "candidate:e2e:obl_action_outcome_admin_high",
        "candidate:e2e:obl_action_outcome_admin_low",
    ]
    assert [item.selection_order for item in selected] == [1, 2]


def test_duplicate_suppression_keeps_one_candidate_and_records_unselected_reasons():
    obligations = [
        _obligation_payload(
            obligation_id="obl:role_sequence:admin:dashboard",
            goal="Reach dashboard.",
            risk_level="P0",
        ),
        _obligation_payload(
            obligation_id="obl:action_outcome:admin:export",
            kind="action_outcome",
            goal="Export the report.",
            risk_level="P2",
        ),
    ]
    candidates = [
        GeneratedE2ECandidateRecord(
            candidate_id="candidate:e2e:combined",
            obligation_ids=[
                "obl:role_sequence:admin:dashboard",
                "obl:action_outcome:admin:export",
            ],
            actor="admin",
            journey_or_flow="dashboard export",
            risk_level="P0",
            reason="covers both obligations",
            recommended_test_type="browser_role_sequence",
        ),
        GeneratedE2ECandidateRecord(
            candidate_id="candidate:e2e:dashboard_only",
            obligation_ids=["obl:role_sequence:admin:dashboard"],
            actor="admin",
            journey_or_flow="dashboard export",
            risk_level="P0",
            reason="duplicate subset",
            recommended_test_type="browser_role_sequence",
        ),
        GeneratedE2ECandidateRecord(
            candidate_id="candidate:e2e:export_only",
            obligation_ids=["obl:action_outcome:admin:export"],
            actor="admin",
            journey_or_flow="dashboard export",
            risk_level="P2",
            reason="duplicate subset",
            recommended_test_type="browser_action_outcome",
        ),
    ]

    payload = candidate_selection_payload(obligations, candidates=candidates, today="2026-05-27")

    assert [item["candidate_id"] for item in payload["selected_e2e_suite"]] == [
        "candidate:e2e:combined"
    ]
    assert {item["candidate_id"] for item in payload["unselected_e2e_candidates"]} == {
        "candidate:e2e:dashboard_only",
        "candidate:e2e:export_only",
    }
    assert {
        item["unselected_reason_code"] for item in payload["unselected_e2e_candidates"]
    } == {"covered_by_selected_candidate"}


def test_lower_level_delegation_is_excluded_with_trace_reason():
    obligations = [
        _obligation_payload(
            coverage_status="covered_by_lower_test",
            covered_by=[{"type": "api_test", "ref": "tests/api/test_contract.py"}],
        )
    ]

    payload = candidate_selection_payload(obligations, today="2026-05-27")

    assert payload["generated_e2e_candidates"] == []
    assert payload["selected_e2e_suite"] == []
    assert payload["excluded_obligations"][0]["reason_code"] == "delegated_to_lower_test"
    assert payload["trace_matrix"][0]["excluded_reason"] == "delegated_to_lower_test"
    assert "lower-level" in payload["trace_matrix"][0]["exclusion_reason"]


def test_active_waiver_is_excluded_but_expired_waiver_is_candidate():
    active = _obligation_payload(
        obligation_id="obl:role_sequence:admin:active_waiver",
        coverage_status="waived_with_reason_and_expiry",
        waiver_reason="Covered next iteration.",
        waiver_expiry="2099-01-01",
    )
    expired = _obligation_payload(
        obligation_id="obl:role_sequence:admin:expired_waiver",
        coverage_status="waived_with_reason_and_expiry",
        waiver_reason="Temporary exception.",
        waiver_expiry="2026-01-01",
    )

    payload = candidate_selection_payload([active, expired], today="2026-05-27")

    assert [item["obligation_id"] for item in payload["excluded_obligations"]] == [
        "obl:role_sequence:admin:active_waiver"
    ]
    assert [item["obligation_ids"] for item in payload["generated_e2e_candidates"]] == [
        ["obl:role_sequence:admin:expired_waiver"]
    ]
    assert "expired_waiver" in payload["generated_e2e_candidates"][0]["reason_codes"]


def test_skip_evidence_is_not_green_and_appears_in_candidate_reason():
    obligations = [
        _obligation_payload(
            coverage_status="covered_by_e2e",
            covered_by=[
                {
                    "type": "verification_test",
                    "ref": "tests/e2e/dashboard.spec.ts",
                    "status": "SKIP",
                }
            ],
        )
    ]

    candidates = generate_e2e_candidates(obligations, today="2026-05-27")

    assert len(candidates) == 1
    assert "skip_evidence" in candidates[0].reason_codes
    assert "green coverage" in candidates[0].reason


def test_deterministic_ordering_is_stable_for_reordered_input():
    first = [
        _obligation_payload(
            obligation_id="obl:action_outcome:admin:publish",
            kind="action_outcome",
            goal="Publish content.",
            risk_level="P2",
        ),
        _obligation_payload(
            obligation_id="obl:global_action:admin:sign_out",
            kind="global_action",
            goal="Sign out.",
            risk_level="P1",
        ),
    ]
    second = list(reversed(first))

    payload_a = candidate_selection_payload(first, today="2026-05-27")
    payload_b = candidate_selection_payload(second, today="2026-05-27")

    assert [item["candidate_id"] for item in payload_a["generated_e2e_candidates"]] == [
        item["candidate_id"] for item in payload_b["generated_e2e_candidates"]
    ]
    assert [item["candidate_id"] for item in payload_a["selected_e2e_suite"]] == [
        item["candidate_id"] for item in payload_b["selected_e2e_suite"]
    ]
