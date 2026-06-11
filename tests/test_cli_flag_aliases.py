"""``--path``/``--project-path`` alias equivalence and ``--json`` deprecation (RF3)."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from codd.cli import main

_CLICK_VERSION = tuple(int(part) for part in click.__version__.split(".")[:2])


def _split_stream_runner() -> CliRunner:
    """CliRunner that keeps stdout/stderr separate across the supported click range."""
    if _CLICK_VERSION < (8, 2):
        return CliRunner(mix_stderr=False)
    return CliRunner()


_CODD_YAML = """\
project_name: demo
wave_config:
  "1":
    - node_id: "design:overview"
      output: "docs/design/overview.md"
      title: "Overview"
"""


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "codd").mkdir()
    (tmp_path / "codd" / "codd.yaml").write_text(_CODD_YAML, encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: ci\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        encoding="utf-8",
    )
    return tmp_path


# One command per affected group/pattern: plain main commands that previously
# used `--path` only, group commands that previously used `--project-path,
# --path`, and the new aggregated `check`.
ALIAS_COMMANDS = [
    ["doctor"],
    ["version", "--check"],
    ["check"],
    ["contract", "verify"],
    ["dag", "verify"],
    ["policy"],
    ["lexicon", "list"],
    ["plan", "--waves"],
    ["measure"],
]


@pytest.mark.parametrize("argv", ALIAS_COMMANDS, ids=lambda argv: " ".join(argv))
def test_path_and_project_path_are_equivalent(project: Path, argv: list[str]) -> None:
    via_path = CliRunner().invoke(main, [*argv, "--path", str(project)])
    via_project_path = CliRunner().invoke(main, [*argv, "--project-path", str(project)])

    assert via_path.exit_code == via_project_path.exit_code
    assert via_path.output == via_project_path.output


@pytest.mark.parametrize("argv", ALIAS_COMMANDS, ids=lambda argv: " ".join(argv))
def test_help_documents_both_aliases(argv: list[str]) -> None:
    result = CliRunner().invoke(main, [*argv, "--help"])

    assert result.exit_code == 0
    assert "--path, --project-path" in result.output
    assert "Project root directory" in result.output


def test_mcp_server_keeps_project_alias() -> None:
    result = CliRunner().invoke(main, ["mcp-server", "--help"])

    assert result.exit_code == 0
    assert "--path, --project-path, --project" in result.output


@pytest.mark.parametrize("argv", [["coverage"], ["plan"], ["measure"]], ids=lambda argv: argv[0])
def test_deprecated_json_flag_matches_format_json_and_warns(project: Path, argv: list[str]) -> None:
    runner = _split_stream_runner()

    via_format = runner.invoke(main, [*argv, "--path", str(project), "--format", "json"])
    via_json = runner.invoke(main, [*argv, "--path", str(project), "--json"])

    assert via_json.exit_code == via_format.exit_code
    assert via_json.stdout == via_format.stdout
    json.loads(via_json.stdout)  # stdout stays machine-readable
    assert "'--json' is deprecated" in via_json.stderr
    assert "deprecated" not in via_format.stderr


@pytest.mark.parametrize("argv", [["coverage"], ["plan"], ["measure"]], ids=lambda argv: argv[0])
def test_deprecated_json_flag_is_hidden_from_help(argv: list[str]) -> None:
    result = CliRunner().invoke(main, [*argv, "--help"])

    assert result.exit_code == 0
    assert "--format [text|json]" in result.output
    assert "--json" not in result.output
