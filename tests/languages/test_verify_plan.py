"""v2.69a — verify run plan + semantic-class classifier (shadow only).

Proves the contract-derived verify plan and the pure semantic classifier match
the legacy verifier's classification on fixtures (or are intentionally
stricter), BEFORE the v2.69b switch. The classifier never re-derives the
anti-false-green heuristics; it maps caller-supplied signals to a class, with
every not-green verdict ordered before the exit-0 PASS.
"""

from __future__ import annotations

import pytest

from codd.languages import default_registry
from codd.languages.contract import build_language_contract
from codd.languages.verify_plan import (
    ShadowComparison,
    VerifyClass,
    VerifyOutcome,
    build_verify_plan,
    classify_verify_outcome,
    shadow_compare,
)


def _contract(language: str):
    return build_language_contract(default_registry.resolve(language))


# ── build_verify_plan ──────────────────────────────────────


def test_go_verify_plan_from_profile():
    plan = build_verify_plan(_contract("go"))
    assert plan is not None
    assert plan.argv == ("go", "test", "-json", "./...")
    assert plan.command_str == "go test -json ./..."
    assert plan.env.get("GOFLAGS") == "-mod=readonly"
    assert plan.report_path == ".codd/verify/go-test.jsonl"
    assert plan.report_adapter == "go-test-json"
    assert plan.report_required is True
    assert set(plan.must_include_test_sets) == {"colocated", "e2e"}


@pytest.mark.parametrize("language", ["go", "python", "typescript"])
def test_all_profiles_yield_a_verify_plan(language):
    plan = build_verify_plan(_contract(language))
    assert plan is not None
    assert plan.argv  # non-empty command
    assert plan.report_adapter  # all three declare a report adapter


# ── classifier: the six semantic classes ──────────────────


def test_pass_on_exit_zero():
    assert classify_verify_outcome(VerifyOutcome(spawned=True, returncode=0)) is VerifyClass.PASS


def test_fail_on_nonzero_exit():
    assert classify_verify_outcome(VerifyOutcome(spawned=True, returncode=1)) is VerifyClass.FAIL


def test_timeout_is_fail_not_green():
    out = VerifyOutcome(spawned=True, returncode=None, timed_out=True)
    assert classify_verify_outcome(out) is VerifyClass.FAIL


def test_tool_missing_when_not_spawned():
    assert classify_verify_outcome(VerifyOutcome(spawned=False)) is VerifyClass.TOOL_MISSING


def test_zero_tests_beats_exit_zero():
    # A runner that exits 0 having collected 0 tests is ZERO_TESTS, never PASS.
    out = VerifyOutcome(spawned=True, returncode=0, zero_tests_observed=True)
    assert classify_verify_outcome(out) is VerifyClass.ZERO_TESTS


def test_config_error_beats_exit_zero():
    out = VerifyOutcome(spawned=True, returncode=0, config_error=True)
    assert classify_verify_outcome(out) is VerifyClass.CONFIG_ERROR


def test_report_missing_when_required_and_absent():
    plan = build_verify_plan(_contract("go"))  # report_required=True
    out = VerifyOutcome(spawned=True, returncode=0, report_present=False)
    assert classify_verify_outcome(out, plan=plan) is VerifyClass.REPORT_MISSING


def test_report_present_passes():
    plan = build_verify_plan(_contract("go"))
    out = VerifyOutcome(spawned=True, returncode=0, report_present=True)
    assert classify_verify_outcome(out, plan=plan) is VerifyClass.PASS


def test_no_report_requirement_does_not_trigger_report_missing():
    # No plan / no required report → exit 0 is a plain PASS (no false REPORT_MISSING).
    out = VerifyOutcome(spawned=True, returncode=0, report_present=False)
    assert classify_verify_outcome(out, plan=None) is VerifyClass.PASS


def test_only_pass_is_green():
    greens = [c for c in VerifyClass if c.is_green]
    assert greens == [VerifyClass.PASS]


# ── shadow comparison (no behaviour change) ───────────────


def test_shadow_go_documents_stricter_profile_but_classes_match():
    # Legacy detect_test_command for Go is `go test ./...`; the profile verify is
    # `go test -json ./...` (stricter — adds a machine-readable report). The
    # COMMANDS differ (documented), but for a passing run both classify PASS.
    contract = _contract("go")
    legacy_pass = VerifyOutcome(spawned=True, returncode=0, report_present=True)
    cmp = shadow_compare(contract, legacy_command="go test ./...", legacy_outcome=legacy_pass)
    assert isinstance(cmp, ShadowComparison)
    assert cmp.commands_identical is False
    assert "stricter" in cmp.note
    # legacy (no plan) and profile (plan) both PASS on a clean run with report.
    assert cmp.legacy_class is VerifyClass.PASS
    assert cmp.profile_class is VerifyClass.PASS
    assert cmp.classes_match is True


def test_shadow_fail_run_classes_match():
    contract = _contract("go")
    failing = VerifyOutcome(spawned=True, returncode=1)
    cmp = shadow_compare(contract, legacy_command="go test ./...", legacy_outcome=failing)
    assert cmp.legacy_class is VerifyClass.FAIL
    assert cmp.profile_class is VerifyClass.FAIL
    assert cmp.classes_match is True


def test_shadow_trace_has_fields():
    contract = _contract("go")
    cmp = shadow_compare(
        contract,
        legacy_command="go test ./...",
        legacy_outcome=VerifyOutcome(spawned=True, returncode=0, report_present=True),
    )
    trace = cmp.to_trace()
    assert trace["shadow_language_id"] == "go"
    assert trace["profile_command"] == "go test -json ./..."
    assert trace["verify_classes_match"] is True
