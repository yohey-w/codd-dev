from __future__ import annotations

from dataclasses import dataclass

from click.testing import CliRunner

from codd import screen_flow_validator, validator
from codd.cli import CoddCLIError, main
from codd.coverage_metrics import (
    compute_dag_completeness,
    compute_design_token_coverage,
    compute_e2e_coverage,
    compute_lexicon_compliance,
    compute_screen_flow_coverage,
    run_coverage,
)


@dataclass(frozen=True)
class _DesignTokenViolation:
    file: str = "App.tsx"
    line: int = 1
    pattern: str = "#123456"
    suggestion: str = "colors.Primary"


def _write_scenarios(project_root, names: list[str]) -> None:
    scenarios = project_root / "docs" / "e2e" / "scenarios.md"
    scenarios.parent.mkdir(parents=True)
    _write_scenarios_file(scenarios, names)


def _write_scenarios_file(scenarios, names: list[str]) -> None:
    scenarios.parent.mkdir(parents=True, exist_ok=True)
    sections = ["# E2E Scenarios", ""]
    for index, name in enumerate(names, 1):
        sections.extend(
            [
                f"## {index}. {name}",
                "- Priority: medium",
                "- Routes: `/login`",
                "",
                "### Steps",
                "1. Open /login.",
                "",
                "### Acceptance Criteria",
                "- Page is visible.",
                "",
            ]
        )
    scenarios.write_text("\n".join(sections), encoding="utf-8")


def test_e2e_coverage_no_scenarios(tmp_path):
    result = compute_e2e_coverage(tmp_path)

    assert result.total == 0
    assert result.pct == 100.0
    assert result.passed is True


def test_e2e_coverage_with_tests(tmp_path):
    _write_scenarios(tmp_path, ["Learner login via /login", "Dashboard view"])
    tests_dir = tmp_path / "docs" / "e2e" / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_learner_login_via_login.spec.ts").write_text("", encoding="utf-8")
    (tests_dir / "test_dashboard_view.cy.ts").write_text("", encoding="utf-8")

    result = compute_e2e_coverage(tmp_path)

    assert result.total == 2
    assert result.covered == 2
    assert result.uncovered == 0
    assert result.pct == 100.0
    assert result.passed is True


def test_e2e_coverage_counts_e2e_ts_stems(tmp_path):
    # ``.e2e.ts`` is a genuine e2e naming convention; such a file must count as a
    # covering e2e test (its stem matches the scenario), like ``.spec.ts``.
    _write_scenarios(tmp_path, ["Learner login via /login"])
    tests_dir = tmp_path / "docs" / "e2e" / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_learner_login_via_login.e2e.ts").write_text("", encoding="utf-8")

    result = compute_e2e_coverage(tmp_path)

    assert result.total == 1
    assert result.covered == 1
    assert result.passed is True


def test_e2e_coverage_symlink_test_escaping_root_not_credited(tmp_path):
    # A test file inside docs/e2e/tests/ that is a SYMLINK pointing to an
    # off-root .spec.ts must NOT be credited as covering its scenario. Pre-fix
    # the glob match was used by name without re-confining, so an out-of-root
    # symlinked spec counted toward coverage (passed=True) — a path-escape
    # false-green crediting a file outside the project tree.
    import pytest

    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_scenarios(project_root, ["Learner login via /login"])
    tests_dir = project_root / "docs" / "e2e" / "tests"
    tests_dir.mkdir()

    # The real spec lives OUTSIDE the project root; its stem matches the scenario.
    outside_spec = tmp_path / "outside" / "test_learner_login_via_login.spec.ts"
    outside_spec.parent.mkdir(parents=True)
    outside_spec.write_text("", encoding="utf-8")
    link = tests_dir / "test_learner_login_via_login.spec.ts"
    try:
        link.symlink_to(outside_spec)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    result = compute_e2e_coverage(project_root)

    # The off-root symlinked spec is dropped: scenario stays uncovered.
    assert result.total == 1
    assert result.covered == 0
    assert result.uncovered == 1
    assert result.passed is False


def test_e2e_coverage_in_root_symlink_still_credited(tmp_path):
    # Anti-false-red regression: a symlink inside docs/e2e/tests/ whose target
    # stays INSIDE the project root is a legitimate in-root test and must still
    # be credited (the jail drops only escaping targets, not all symlinks).
    import pytest

    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_scenarios(project_root, ["Learner login via /login"])
    tests_dir = project_root / "docs" / "e2e" / "tests"
    tests_dir.mkdir()

    # Real spec lives elsewhere IN-ROOT; symlinked into the tests dir.
    real_spec = project_root / "generated" / "test_learner_login_via_login.spec.ts"
    real_spec.parent.mkdir(parents=True)
    real_spec.write_text("", encoding="utf-8")
    link = tests_dir / "test_learner_login_via_login.spec.ts"
    try:
        link.symlink_to(real_spec)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    result = compute_e2e_coverage(project_root)

    assert result.total == 1
    assert result.covered == 1
    assert result.passed is True


def test_e2e_coverage_scenarios_symlink_escaping_root_yields_no_scenarios(tmp_path):
    # The scenarios.md path is hardcoded in-root, but if it is itself a symlink
    # whose target escapes the project root, an off-root markdown must NOT be
    # consumed as the scenario source. Pre-fix the path was read without
    # re-confining (a per-file symlink escape false-green).
    import pytest

    project_root = tmp_path / "project"
    (project_root / "docs" / "e2e").mkdir(parents=True)

    outside_scenarios = tmp_path / "outside_scenarios.md"
    _write_scenarios_file(outside_scenarios, ["Smuggled scenario"])
    link = project_root / "docs" / "e2e" / "scenarios.md"
    try:
        link.symlink_to(outside_scenarios)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    result = compute_e2e_coverage(project_root)

    # Off-root scenarios source dropped -> behaves as if there are no scenarios.
    assert result.total == 0
    assert result.pct == 100.0
    assert result.passed is True


def test_e2e_coverage_missing_tests(tmp_path):
    _write_scenarios(tmp_path, ["Login", "Dashboard", "Settings"])

    result = compute_e2e_coverage(tmp_path)

    assert result.total == 3
    assert result.covered == 0
    assert result.uncovered == 3
    assert result.pct == 0.0
    assert result.passed is False
    assert any("missing tests:" in detail for detail in result.details)


def test_lexicon_compliance_no_violations(tmp_path, monkeypatch):
    monkeypatch.setattr(validator, "validate_with_lexicon", lambda project_root: [])

    result = compute_lexicon_compliance(tmp_path)

    assert result.pct == 100.0
    assert result.passed is True


def test_lexicon_compliance_with_violations(tmp_path, monkeypatch):
    monkeypatch.setattr(
        validator,
        "validate_with_lexicon",
        lambda project_root: [{"node_id": "url_route", "message": "bad convention"}],
    )

    result = compute_lexicon_compliance(tmp_path)

    assert result.pct == 0.0
    assert result.uncovered == 1
    assert result.passed is False


def test_design_token_coverage_default_threshold_is_informational(tmp_path, monkeypatch):
    monkeypatch.setattr(
        validator,
        "validate_design_tokens",
        lambda project_root: [_DesignTokenViolation()],
    )

    result = compute_design_token_coverage(tmp_path)

    assert result.pct == 0.0
    assert result.uncovered == 1
    assert result.threshold == 0.0
    assert result.passed is True


def test_screen_flow_coverage_no_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(screen_flow_validator, "validate_screen_flow", lambda project_root, config: [])

    result = compute_screen_flow_coverage(tmp_path, {}, threshold=100.0)

    assert result.metric == "screen_flow_coverage"
    assert result.pct == 100.0
    assert result.uncovered == 0
    assert result.passed is True
    assert result.details == ["drift_count: 0"]


def test_screen_flow_coverage_with_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(
        screen_flow_validator,
        "validate_screen_flow",
        lambda project_root, config: [object(), object()],
    )

    result = compute_screen_flow_coverage(tmp_path, {}, threshold=95.0)

    assert result.pct == 80.0
    assert result.uncovered == 2
    assert result.passed is False
    assert result.details == ["drift_count: 2"]


def test_screen_flow_coverage_coddclierror(tmp_path, monkeypatch):
    def raise_misconfigured_base_dir(project_root, config):
        raise CoddCLIError("bad filesystem_routes.base_dir")

    monkeypatch.setattr(screen_flow_validator, "validate_screen_flow", raise_misconfigured_base_dir)

    result = compute_screen_flow_coverage(tmp_path, {"filesystem_routes": [{"base_dir": "app"}]})

    assert result.pct == 0.0
    assert result.passed is False
    assert result.uncovered == 1
    assert result.details == ["error: bad filesystem_routes.base_dir"]


_CI_HEALTH_OPT_OUT_CONFIG = {
    "ci": {"provider": "none"},
    "opt_outs": [
        {
            "check": "ci_health",
            "reason": "no CI provider configured for this empty fixture",
            "expires_at": "2099-12-31",
        }
    ]
}


def test_dag_completeness_empty_project_passes(tmp_path):
    # An empty project must opt out of C8 ci_health explicitly; otherwise the
    # check correctly red-fails on the missing workflow. The gate-level
    # invariant verified here is "empty edge case does not crash and the
    # opt-out path passes coverage".
    result = compute_dag_completeness(tmp_path, config=_CI_HEALTH_OPT_OUT_CONFIG)

    assert result.metric == "dag_completeness"
    assert result.passed is True
    assert result.uncovered == 0


def test_dag_completeness_empty_project_without_opt_out_fails_ci_health(tmp_path):
    result = compute_dag_completeness(tmp_path)

    assert result.metric == "dag_completeness"
    assert result.passed is False
    assert result.uncovered == 1
    assert any("ci_health" in detail for detail in result.details)


def test_run_coverage_all_pass(tmp_path):
    report = run_coverage(tmp_path, config=_CI_HEALTH_OPT_OUT_CONFIG)

    assert report.all_passed is True
    assert [result.metric for result in report.results] == [
        "e2e_coverage",
        "design_token_coverage",
        "lexicon_compliance",
        "screen_flow_coverage",
        "dag_completeness",
    ]


def test_run_coverage_includes_screen_flow(tmp_path):
    report = run_coverage(tmp_path)

    assert any(result.metric == "screen_flow_coverage" for result in report.results)


def test_run_coverage_includes_dag_completeness(tmp_path):
    report = run_coverage(tmp_path)

    assert any(result.metric == "dag_completeness" for result in report.results)


def test_cli_coverage_help():
    result = CliRunner().invoke(main, ["coverage", "--help"])

    assert result.exit_code == 0
    assert "Coverage metrics merge gate" in result.output


def test_cli_coverage_returns_exit_1_on_gate_failure(tmp_path):
    _write_scenarios(tmp_path, ["Login"])

    result = CliRunner().invoke(main, ["coverage", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "[FAIL] e2e_coverage" in result.output
    assert "Coverage gate FAILED" in result.output
