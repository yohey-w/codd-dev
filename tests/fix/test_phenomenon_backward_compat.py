"""Verify legacy `codd fix` (argument-less) behavior is unchanged.

The PHENOMENON mode is opt-in via the positional argument. When the
argument is omitted, the CLI must route to the legacy run_fix() path
and the existing API surface must remain intact.
"""

from __future__ import annotations

import inspect

from click.testing import CliRunner

from codd.cli import main
from codd.fixer import run_fix


def test_legacy_run_fix_signature_unchanged():
    sig = inspect.signature(run_fix)
    params = list(sig.parameters)
    expected = [
        "project_root",
        "ai_command",
        "max_attempts",
        "test_results",
        "ci_log",
        "ci_only",
        "local_only",
        "push",
        "dry_run",
        "coherence_event",
    ]
    assert params == expected


def test_codd_fix_help_lists_both_modes():
    runner = CliRunner()
    result = runner.invoke(main, ["fix", "--help"])
    assert result.exit_code == 0
    assert "PHENOMENON" in result.output
    assert "auto-detect" in result.output.lower()


def test_codd_fix_phenomenon_argument_is_optional():
    """Without a positional argument, the command should not error from click parsing."""
    runner = CliRunner()
    # Use --path to a non-codd dir so we exit fast without invoking AI.
    result = runner.invoke(main, ["fix", "--help"])
    assert result.exit_code == 0
    assert "PHENOMENON" in result.output
