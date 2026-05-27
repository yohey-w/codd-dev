from __future__ import annotations

from codd.coverage_obligations import (
    CoverageStatus,
    ObligationKind,
    RiskLevel,
    SourceType,
    coverage_obligation_from_mapping,
    is_incomplete_coverage,
    normalize_coverage_status,
)


def _obligation_payload(**overrides):
    payload = {
        "obligation_id": "obl:role_sequence:admin:dashboard",
        "source": {
            "type": "design_doc",
            "ref": "docs/design/admin.md#user_journeys[admin_dashboard]",
        },
        "kind": "role_sequence",
        "actor": "admin",
        "goal": "Reach the dashboard through the browser flow.",
        "preconditions": ["admin is authenticated"],
        "expected_outcomes": ["dashboard is visible"],
        "side_effects": [],
        "risk_level": "P1",
        "coverage_status": "covered_by_e2e",
        "covered_by": [
            {
                "type": "verification_test",
                "ref": "tests/e2e/admin_dashboard.spec.ts",
            }
        ],
        "waiver_reason": None,
        "waiver_expiry": None,
    }
    payload.update(overrides)
    return payload


def test_coverage_obligation_from_mapping_normalizes_required_schema_fields():
    obligation = coverage_obligation_from_mapping(_obligation_payload(), today="2026-05-27")

    assert obligation.obligation_id == "obl:role_sequence:admin:dashboard"
    assert obligation.source.type == SourceType.DESIGN_DOC
    assert obligation.kind == ObligationKind.ROLE_SEQUENCE
    assert obligation.risk_level == RiskLevel.P1
    assert obligation.coverage_status == CoverageStatus.COVERED_BY_E2E
    assert obligation.preconditions == ("admin is authenticated",)
    assert obligation.expected_outcomes == ("dashboard is visible",)
    assert obligation.covered_by[0].ref == "tests/e2e/admin_dashboard.spec.ts"


def test_status_normalization_accepts_all_four_canonical_statuses():
    assert (
        normalize_coverage_status(
            "covered-by-e2e",
            covered_by=["tests/e2e/admin_dashboard.spec.ts"],
        )
        == CoverageStatus.COVERED_BY_E2E
    )
    assert (
        normalize_coverage_status(
            "covered_by_lower_test",
            covered_by=[{"type": "unit_test", "ref": "tests/test_formatter.py"}],
        )
        == CoverageStatus.COVERED_BY_LOWER_TEST
    )
    assert (
        normalize_coverage_status(
            "waived",
            waiver_reason="Not in this release scope.",
            waiver_expiry="2099-01-01",
            today="2026-05-27",
        )
        == CoverageStatus.WAIVED_WITH_REASON_AND_EXPIRY
    )
    assert normalize_coverage_status("uncovered") == CoverageStatus.UNCOVERED


def test_skip_evidence_is_incomplete_and_uncovered():
    status = normalize_coverage_status(
        "covered_by_e2e",
        covered_by=[
            {
                "type": "verification_test",
                "ref": "tests/e2e/admin_dashboard.spec.ts",
                "status": "SKIP",
            }
        ],
    )

    assert status == CoverageStatus.UNCOVERED
    assert (
        normalize_coverage_status(
            "covered_by_e2e",
            covered_by=[
                "tests/e2e/admin_dashboard.spec.ts",
                {
                    "type": "verification_test",
                    "ref": "tests/e2e/admin_dashboard_mobile.spec.ts",
                    "status": "skipped",
                },
            ],
        )
        == CoverageStatus.UNCOVERED
    )
    assert is_incomplete_coverage(
        "covered_by_e2e",
        covered_by=[
            {
                "type": "verification_test",
                "ref": "tests/e2e/admin_dashboard.spec.ts",
                "skipped": True,
            }
        ],
    )


def test_implicit_opt_out_is_incomplete_and_uncovered():
    assert normalize_coverage_status(None) == CoverageStatus.UNCOVERED
    assert normalize_coverage_status("opt_out") == CoverageStatus.UNCOVERED
    assert is_incomplete_coverage("not_applicable")


def test_expired_or_reasonless_waiver_is_incomplete_and_uncovered():
    assert (
        normalize_coverage_status(
            "waived_with_reason_and_expiry",
            waiver_reason="Temporarily outside release scope.",
            waiver_expiry="2026-01-01",
            today="2026-05-27",
        )
        == CoverageStatus.UNCOVERED
    )
    assert (
        normalize_coverage_status(
            "waived_with_reason_and_expiry",
            waiver_reason=None,
            waiver_expiry="2099-01-01",
            today="2026-05-27",
        )
        == CoverageStatus.UNCOVERED
    )


def test_lower_level_delegation_counts_as_covered_by_lower_test():
    obligation = coverage_obligation_from_mapping(
        _obligation_payload(
            kind="lower_level_contract",
            coverage_status="covered_by_lower_test",
            covered_by=[{"type": "api_test", "ref": "tests/api/test_contract.py"}],
        ),
        today="2026-05-27",
    )

    assert obligation.kind == ObligationKind.LOWER_LEVEL_CONTRACT
    assert obligation.coverage_status == CoverageStatus.COVERED_BY_LOWER_TEST
    assert not obligation.is_incomplete(today="2026-05-27")


def test_future_candidate_and_suite_stubs_do_not_create_green_coverage():
    obligation = coverage_obligation_from_mapping(
        _obligation_payload(
            coverage_status="uncovered",
            covered_by=[],
            generated_e2e_candidates=[
                {
                    "candidate_id": "candidate:admin_dashboard",
                    "covers": ["obl:role_sequence:admin:dashboard"],
                }
            ],
            selected_e2e_suite=[
                {
                    "suite_id": "suite:smoke",
                    "candidate_ids": ["candidate:admin_dashboard"],
                }
            ],
        ),
        today="2026-05-27",
    )

    assert obligation.generated_e2e_candidates[0].candidate_id == "candidate:admin_dashboard"
    assert obligation.selected_e2e_suite[0].suite_id == "suite:smoke"
    assert obligation.coverage_status == CoverageStatus.UNCOVERED
    assert obligation.is_incomplete(today="2026-05-27")
