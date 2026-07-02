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
    # cwd placeholder {module_root} must be SUBSTITUTED to the layout root ("."),
    # not passed literally — an unsubstituted cwd makes the executor spawn in a
    # nonexistent <project>/{module_root} dir (a spurious TOOL_MISSING; v2.75 bug,
    # caught only by real-go validation, fixed v2.76).
    assert plan.cwd == "."
    assert "{module_root}" not in (plan.cwd or "")
    assert plan.env.get("GOFLAGS") == "-mod=readonly -buildvcs=false"
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


# REGRESSION (found while dogfooding ``codd verify`` on a generated TypeScript
# project, ExprCalcTs): TypeScript's (and JavaScript's) ``commands.verify.argv``
# declares ``{test_root}``/``{report}`` template placeholders, but
# ``build_verify_plan`` used to copy ``cmd.argv`` VERBATIM — only ``cwd``/``env``
# went through placeholder substitution. The spawned command was therefore
# LITERALLY ``vitest run {test_root} --outputFile={report}``: vitest collected zero
# tests (a nonexistent path filter) and wrote its report to a file literally named
# ``{report}`` in the cwd, never to the declared ``report_path`` — a false
# REPORT_MISSING on a project whose tests all genuinely passed. Prove every
# profile's plan is fully substituted, not just the two (go/python) that never
# happened to declare these placeholders in the first place.
@pytest.mark.parametrize("language", [p.identity.id for p in default_registry.all_profiles()])
def test_verify_plan_argv_has_no_unsubstituted_placeholder(language):
    plan = build_verify_plan(_contract(language))
    if plan is None:
        pytest.skip(f"{language} profile declares no verify command")
    known_placeholders = ("{module_root}", "{repo_root}", "{manifest_root}", "{test_root}", "{report}")
    for arg in plan.argv:
        for token in known_placeholders:
            assert token not in arg, f"{language} verify argv still carries {token!r}: {plan.argv!r}"
    for token in known_placeholders:
        assert token not in (plan.cwd or ""), f"{language} verify cwd still carries {token!r}: {plan.cwd!r}"


def test_typescript_verify_plan_substitutes_test_root_and_report():
    plan = build_verify_plan(_contract("typescript"))
    assert plan is not None
    assert plan.argv == (
        "npx",
        "--no-install",
        "vitest",
        "run",
        "tests",
        "--reporter=json",
        "--outputFile=.codd/verify/vitest.json",
    )
    assert plan.command_str == (
        "npx --no-install vitest run tests --reporter=json --outputFile=.codd/verify/vitest.json"
    )
    assert plan.report_path == ".codd/verify/vitest.json"
    # {report} must resolve to EXACTLY report_path, or the executor's own report
    # read and the spawned command's --outputFile would silently name two different
    # files (a false REPORT_MISSING, or a stale report read as this run's result).
    assert f"--outputFile={plan.report_path}" in plan.argv


def test_javascript_verify_plan_substitutes_test_root_and_report():
    # javascript.yaml is a genuinely separate profile from typescript.yaml (not an
    # alias) that declares the identical {test_root}/{report} verify.argv shape —
    # the fix must be general across BOTH, not special-cased to "typescript".
    plan = build_verify_plan(_contract("javascript"))
    assert plan is not None
    assert "{test_root}" not in plan.command_str
    assert "{report}" not in plan.command_str
    assert f"--outputFile={plan.report_path}" in plan.argv


def test_ambiguous_test_root_leaves_placeholder_for_red():
    # A profile declaring zero or multiple test sets cannot resolve {test_root}
    # unambiguously — the placeholder must survive (not be silently erased) so the
    # executor's unsubstituted-placeholder guard reds rather than guesses.
    from codd.languages.verify_plan import _resolve_test_root, _substitute_test_command_placeholders

    class _FakeLayout:
        def __init__(self, test_sets):
            self.test_sets = test_sets

    class _FakeTestSet:
        def __init__(self, root):
            self.root = root

    assert _resolve_test_root(_FakeLayout(())) == ""  # zero declared → ambiguous
    assert _resolve_test_root(_FakeLayout((_FakeTestSet("a"), _FakeTestSet("b")))) == ""  # multiple → ambiguous
    assert _resolve_test_root(_FakeLayout((_FakeTestSet("tests"),))) == "tests"  # exactly one → ok

    resolved = _substitute_test_command_placeholders(
        "vitest run {test_root} --outputFile={report}",
        test_root="",
        report_path=".codd/verify/vitest.json",
    )
    assert "{test_root}" in resolved, "an ambiguous test root must survive substitution, never be erased"


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
