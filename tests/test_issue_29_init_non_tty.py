"""Regression tests for Issue #29 — `codd init <name>` aborts in non-TTY shells.

`codd init` declared the project name (and primary language) as
``click.option(..., prompt=...)``. When the value was omitted, click fired an
interactive prompt; with no controlling terminal (CI, an agent's Bash tool,
stdin redirected from /dev/null) the prompt immediately aborted, printing a
bare ``Project name: Aborted!`` and exiting non-zero. The only workaround was
to always pass ``--project-name`` explicitly.

The fix makes init usable non-interactively:
- The project name is accepted as a POSITIONAL argument (``codd init <name>``),
  the documented/expected UX.
- ``--project-name`` is kept as a back-compatible alias.
- When a required value is missing and stdin is not a TTY, init fails with a
  clear, actionable ``UsageError`` naming the flag to pass — never an opaque
  ``Aborted!``.
- On a real terminal, interactive prompting still works (covered indirectly:
  the prompt is only reached when ``sys.stdin.isatty()`` is true).

These tests use click's ``CliRunner`` whose default stdin is not a TTY, exactly
reproducing the non-interactive environment from the bug report.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import yaml

from codd.cli import main


def _read_project_name(dest: Path, config_dir: str = "codd") -> str:
    data = yaml.safe_load((dest / config_dir / "codd.yaml").read_text(encoding="utf-8"))
    return (data.get("project") or {}).get("name")


def test_init_positional_name_works_without_tty(tmp_path: Path) -> None:
    """`codd init <name> --language ...` must succeed with no TTY (the repro).

    Before the fix this aborted with exit code 1 and ``Project name: Aborted!``.
    """
    dest = tmp_path / "proj"
    dest.mkdir()

    result = CliRunner().invoke(
        main,
        ["init", "repro", "--language", "python", "--dest", str(dest), "--no-suggest-lexicons"],
    )

    assert result.exit_code == 0, result.output
    assert "Aborted!" not in result.output
    assert _read_project_name(dest) == "repro"


def test_init_project_name_option_still_supported(tmp_path: Path) -> None:
    """Back-compat: the old --project-name flag keeps working non-interactively."""
    dest = tmp_path / "proj"
    dest.mkdir()

    result = CliRunner().invoke(
        main,
        [
            "init",
            "--project-name",
            "legacy",
            "--language",
            "python",
            "--dest",
            str(dest),
            "--no-suggest-lexicons",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Aborted!" not in result.output
    assert _read_project_name(dest) == "legacy"


def test_init_missing_name_non_tty_gives_actionable_error(tmp_path: Path) -> None:
    """No name + no TTY must fail with a clear message, not a bare ``Aborted!``."""
    dest = tmp_path / "proj"
    dest.mkdir()

    result = CliRunner().invoke(
        main,
        ["init", "--language", "python", "--dest", str(dest), "--no-suggest-lexicons"],
    )

    assert result.exit_code != 0
    assert "Aborted!" not in result.output
    assert "Project name is required" in result.output
    # Mentions both ways to supply it so the user can self-serve.
    assert "--project-name" in result.output
    assert "codd init <name>" in result.output
    # Nothing should have been written for an invalid invocation.
    assert not (dest / "codd").exists()


def test_init_missing_language_non_tty_gives_actionable_error(tmp_path: Path) -> None:
    """The language prompt must not abort under non-TTY either."""
    dest = tmp_path / "proj"
    dest.mkdir()

    result = CliRunner().invoke(
        main,
        ["init", "somename", "--dest", str(dest), "--no-suggest-lexicons"],
    )

    assert result.exit_code != 0
    assert "Aborted!" not in result.output
    assert "Primary language is required" in result.output
    assert "--language" in result.output


def test_init_conflicting_names_rejected(tmp_path: Path) -> None:
    """Positional NAME and --project-name disagreeing is an explicit error."""
    dest = tmp_path / "proj"
    dest.mkdir()

    result = CliRunner().invoke(
        main,
        [
            "init",
            "aaa",
            "--project-name",
            "bbb",
            "--language",
            "python",
            "--dest",
            str(dest),
            "--no-suggest-lexicons",
        ],
    )

    assert result.exit_code != 0
    assert "Conflicting project names" in result.output
    assert not (dest / "codd").exists()


def test_init_positional_and_option_agreeing_is_accepted(tmp_path: Path) -> None:
    """Supplying the same name both ways is harmless (idempotent), not an error."""
    dest = tmp_path / "proj"
    dest.mkdir()

    result = CliRunner().invoke(
        main,
        [
            "init",
            "same",
            "--project-name",
            "same",
            "--language",
            "python",
            "--dest",
            str(dest),
            "--no-suggest-lexicons",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _read_project_name(dest) == "same"
