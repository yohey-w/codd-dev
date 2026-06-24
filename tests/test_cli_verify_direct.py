"""CLI checks for direct verify behavior and removed legacy commands."""

import io
from contextlib import redirect_stdout

from click.testing import CliRunner

from codd.cli import _CliVerificationResult, _emit_verify_summary, main


def _summary_output_for(check_results):
    result = _CliVerificationResult(
        passed=True,
        exit_code=0,
        check_results=check_results,
        runtime_results=[],
    )
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        _emit_verify_summary(result)
    return buffer.getvalue()


def test_emit_verify_summary_amber_with_findings_counts_as_warn():
    """A passed amber check carrying findings is summarised as WARN, not PASS.

    Regression for the third verify summary, which bypassed the amber-with-findings
    rule and reported '1 PASS / 0 WARN' — hiding an advisory behind a green PASS.
    """
    output = _summary_output_for(
        [
            {
                "name": "dependency_freshness",
                "passed": True,
                "severity": "amber",
                "warnings": ["dependency X is stale"],
            }
        ]
    )

    assert "DAG checks: 0 PASS / 0 FAIL (red) / 1 WARN (amber)" in output
    assert "1 PASS" not in output.split("DAG checks: ")[1].splitlines()[0]


def test_emit_verify_summary_clean_amber_pass_stays_pass():
    """An amber check with NO findings stays PASS (backward-compatible)."""
    output = _summary_output_for(
        [
            {
                "name": "dependency_freshness",
                "passed": True,
                "severity": "amber",
                "warnings": [],
            }
        ]
    )

    assert "DAG checks: 1 PASS / 0 FAIL (red) / 0 WARN (amber)" in output


def test_verify_help_no_pro_gate_message():
    """codd verify --help does not reference the legacy Pro package."""
    runner = CliRunner()

    result = runner.invoke(main, ["verify", "--help"])

    assert result.exit_code == 0
    assert "codd-pro" not in result.output
    assert "pip install codd-pro" not in result.output


def test_review_command_removed():
    """The legacy review command is removed."""
    runner = CliRunner()

    result = runner.invoke(main, ["review", "--help"])

    assert result.exit_code != 0


def test_audit_command_removed():
    """The legacy audit command is removed."""
    runner = CliRunner()

    result = runner.invoke(main, ["audit", "--help"])

    assert result.exit_code != 0


def test_risk_command_removed():
    """The legacy risk command is removed."""
    runner = CliRunner()

    result = runner.invoke(main, ["risk", "--help"])

    assert result.exit_code != 0
