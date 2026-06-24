"""CLI checks for direct verify behavior and removed legacy commands."""

import io
from contextlib import redirect_stdout
from types import SimpleNamespace

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


def _check(**fields):
    """A check result shaped like the real ones (a dataclass object).

    Production ``check_results`` are dag CheckResult dataclass instances (see
    ``codd/dag/checks/*``), not dicts — the materiality overlay reads
    ``checked_count`` / ``skipped`` off attributes. SimpleNamespace mirrors that.
    """
    return SimpleNamespace(**fields)


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


def test_emit_verify_summary_skipped_check_counts_as_skip_not_pass():
    """A skipped DAG check is counted as SKIP, never as a clean PASS.

    Regression for the third verify summary, which counted any ``passed != False``
    item as PASS — a skipped check (verified nothing) was indistinguishable from a
    real pass, hiding silent skips behind a green PASS (the other two summaries
    already surface SKIP distinctly).
    """
    output = _summary_output_for(
        [
            _check(
                check_name="ui_coherence",
                passed=True,
                severity="red",
                status="skip",
                skipped=True,
            )
        ]
    )

    summary_line = output.split("DAG checks: ")[1].splitlines()[0]
    assert "0 PASS" in summary_line
    assert "1 SKIP" in summary_line
    # The skipped check must NOT be tallied as PASS.
    assert "1 PASS" not in summary_line
    assert "verified nothing (dormant / unconfigured)" in output


def test_emit_verify_summary_vacuous_pass_not_clean_pass():
    """A pass that verified zero items (checked_count==0) is shown as vacuous.

    Regression for the third verify summary, which counted a vacuous pass as a
    clean PASS — with vacuous-closure many checks now return checked_count==0, so
    a riddled-with-vacuous run looked fully green. The other two summaries already
    surface this via the materiality overlay.
    """
    output = _summary_output_for(
        [
            _check(
                check_name="ui_coherence_for_one_to_many",
                passed=True,
                severity="red",
                status="pass",
                checked_count=0,
            )
        ]
    )

    summary_line = output.split("DAG checks: ")[1].splitlines()[0]
    # A vacuous pass is not a clean PASS.
    assert "0 PASS" in summary_line
    assert "1 VACUOUS" in summary_line
    # And it is named in a dedicated vacuous line, matching the other summaries.
    assert "verified nothing (vacuous)" in output
    assert "ui_coherence_for_one_to_many" in output


def test_emit_verify_summary_true_pass_still_counts_as_pass():
    """A genuine pass (findings-free, not skipped, checked_count>0) stays PASS."""
    output = _summary_output_for(
        [
            _check(
                check_name="structural_integrity",
                passed=True,
                severity="red",
                status="pass",
                checked_count=12,
            )
        ]
    )

    summary_line = output.split("DAG checks: ")[1].splitlines()[0]
    assert "DAG checks: 1 PASS / 0 FAIL (red) / 0 WARN (amber)" in output
    # The genuine pass is tallied only as PASS — not as SKIP or VACUOUS.
    assert "0 SKIP" in summary_line
    assert "0 VACUOUS" in summary_line
    # No dedicated skip / vacuous detail lines for a clean run.
    assert "verified nothing" not in output


def test_emit_verify_summary_vacuous_and_warn_not_double_counted():
    """P1 double-count: a result that is BOTH vacuous (checked_count==0) AND an
    amber-with-findings WARN (severity=amber + warnings) must appear ONLY in the
    WARN tally / per-row, never also in the 'verified nothing (vacuous)' list.

    The count logic (dag_vacuous) already excludes ``_dag_pass_is_warn`` results,
    but the displayed vacuous list (``vacuous_pass_results``) did not apply the
    same filter — so the same check was counted as WARN *and* listed as vacuous.
    """
    output = _summary_output_for(
        [
            _check(
                check_name="depends_on_consistency",
                passed=True,
                severity="amber",
                status="pass",
                checked_count=0,
                warnings=["WARN: produced values but none on a depends_on edge (vacuous)"],
            )
        ]
    )

    summary_line = output.split("DAG checks: ")[1].splitlines()[0]
    # It is a WARN, not a VACUOUS, in the tally (count side was already correct).
    assert "1 WARN (amber)" in summary_line
    assert "0 VACUOUS" in summary_line
    # And it must NOT be named in the vacuous list (the display side, the bug).
    assert "verified nothing (vacuous)" not in output


def test_emit_verify_summary_pure_vacuous_still_listed():
    """Regression: a vacuous pass that is NOT a WARN (red severity, no findings)
    still appears in the vacuous list — the P1 filter must only drop the
    overlap, not silence genuine vacuous passes."""
    output = _summary_output_for(
        [
            _check(
                check_name="ui_coherence_for_one_to_many",
                passed=True,
                severity="red",
                status="pass",
                checked_count=0,
            )
        ]
    )

    summary_line = output.split("DAG checks: ")[1].splitlines()[0]
    assert "1 VACUOUS" in summary_line
    assert "verified nothing (vacuous)" in output
    assert "ui_coherence_for_one_to_many" in output


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
