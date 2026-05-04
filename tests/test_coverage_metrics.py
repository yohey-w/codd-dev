from __future__ import annotations

from dataclasses import dataclass

from click.testing import CliRunner

from codd import validator
from codd.cli import main
from codd.coverage_metrics import (
    compute_design_token_coverage,
    compute_e2e_coverage,
    compute_lexicon_compliance,
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


def test_run_coverage_all_pass(tmp_path):
    report = run_coverage(tmp_path)

    assert report.all_passed is True
    assert [result.metric for result in report.results] == [
        "e2e_coverage",
        "design_token_coverage",
        "lexicon_compliance",
    ]


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
