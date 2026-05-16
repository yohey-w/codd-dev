"""CLI checks for direct verify behavior and removed legacy commands."""

from click.testing import CliRunner

from codd.cli import main


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
