"""G2 delivery vehicles for the greenfield autopilot.

Three vehicles, one pipeline:
  A. ``examples/greenfield_autopilot.sh``       — transparent shell composition
  B. ``examples/claude_workflows/codd-greenfield.js`` — Claude Code Agent Workflow
  C. ``skills/codd-greenfield/``                — skill for Claude Code + Codex CLI
plus the requirements-nudge hook recipe and README sync.

These tests validate structure and distribution wiring, not pipeline
behavior (that lives in tests/greenfield/).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from codd.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
SHELL_SCRIPT = REPO_ROOT / "examples" / "greenfield_autopilot.sh"
WORKFLOW_JS = REPO_ROOT / "examples" / "claude_workflows" / "codd-greenfield.js"
EXAMPLES_README = REPO_ROOT / "examples" / "README.md"
SKILL_MD = REPO_ROOT / "skills" / "codd-greenfield" / "SKILL.md"
NUDGE_RECIPE = REPO_ROOT / "codd" / "hooks" / "recipes" / "claude_requirements_nudge.json"


# ═══════════════════════════════════════════════════════════
# A. Shell vehicle
# ═══════════════════════════════════════════════════════════

def test_shell_script_exists_and_is_executable() -> None:
    assert SHELL_SCRIPT.is_file()
    assert os.access(SHELL_SCRIPT, os.X_OK), "examples/greenfield_autopilot.sh must be executable"


def test_shell_script_passes_bash_syntax_check() -> None:
    bash = shutil.which("bash")
    assert bash is not None, "bash is required to validate the shell vehicle"
    result = subprocess.run(
        [bash, "-n", str(SHELL_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


def test_shell_script_composes_the_stage_cli_sequence() -> None:
    """The shell vehicle must compose the exact stage commands the autopilot runs."""
    body = SHELL_SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        "codd elicit",
        "codd plan --init --force",
        "codd generate --all-waves --force",
        "codd implement list-tasks --format json",
        "--approve --all",
        "codd implement run --task",
        "codd verify --auto-repair --max-attempts",
        "--repair-mode automatic",
        "codd propagate --commit",
        "codd check",
        "codd greenfield --resume",  # points users at the one-command equivalent
        "set -euo pipefail",
    ):
        assert fragment in body, f"shell vehicle is missing: {fragment}"


def test_shell_script_help_exits_zero_and_names_the_one_command_form(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(SHELL_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "codd greenfield" in result.stdout


def test_shell_script_rejects_unknown_option(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(SHELL_SCRIPT), "proj", "--bogus"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert result.returncode == 2
    assert "unknown option" in result.stderr


# ═══════════════════════════════════════════════════════════
# B. Claude Code Agent Workflow vehicle
# ═══════════════════════════════════════════════════════════

def test_workflow_template_exists_with_meta_export() -> None:
    assert WORKFLOW_JS.is_file()
    body = WORKFLOW_JS.read_text(encoding="utf-8")
    assert "export const meta" in body
    assert '"codd-greenfield"' in body
    assert "export default" in body


def test_workflow_template_has_balanced_braces_and_parens() -> None:
    body = WORKFLOW_JS.read_text(encoding="utf-8")
    assert body.count("{") == body.count("}"), "unbalanced braces in workflow template"
    assert body.count("(") == body.count(")"), "unbalanced parens in workflow template"


def test_workflow_template_mirrors_the_pipeline_stages() -> None:
    body = WORKFLOW_JS.read_text(encoding="utf-8")
    for stage in ("init", "elicit", "plan", "generate", "implement", "verify", "propagate", "check"):
        assert f'"{stage}"' in body, f"workflow template is missing phase: {stage}"
    assert "codd greenfield --resume" in body  # failure path names the resume command
    assert ".claude/workflows" in body  # install instructions


# ═══════════════════════════════════════════════════════════
# C. Skill + hooks
# ═══════════════════════════════════════════════════════════

def test_skill_md_exists_with_frontmatter_and_gates() -> None:
    assert SKILL_MD.is_file()
    body = SKILL_MD.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "name: codd-greenfield" in body
    assert "codd greenfield --requirements" in body  # canonical flow
    assert "codd greenfield --resume" in body  # recovery
    assert ".codd/greenfield_session.yaml" in body  # session inspection
    assert "Stop-and-Ask Gates" in body
    assert "Do not interrupt the autopilot" in body


def test_skill_is_discovered_from_the_repo_skills_dir() -> None:
    from codd.skills_cli.discovery import find_skill_source

    source = find_skill_source("codd-greenfield")
    assert source == (REPO_ROOT / "skills" / "codd-greenfield").resolve()


def test_skills_install_codd_greenfield_target_both(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    result = CliRunner().invoke(main, ["skills", "install", "codd-greenfield", "--target", "both"])

    assert result.exit_code == 0, result.output
    for dest in (
        home / ".claude" / "skills" / "codd-greenfield",
        home / ".agents" / "skills" / "codd-greenfield",
    ):
        assert dest.is_symlink()
        assert (dest / "SKILL.md").is_file()


def test_requirements_nudge_recipe_is_valid_claude_settings_json() -> None:
    payload = json.loads(NUDGE_RECIPE.read_text(encoding="utf-8"))
    entries = payload["hooks"]["PostToolUse"]
    assert entries, "recipe must register a PostToolUse hook"
    hook = entries[0]["hooks"][0]
    assert hook["type"] == "command"
    assert "codd greenfield --resume" in hook["command"]
    assert "codd check" in hook["command"]


@pytest.mark.parametrize(
    ("tool_input", "expect_nudge"),
    [
        ({"file_path": "docs/requirements/requirements.md"}, True),
        ({"file_paths": ["docs/requirements/auth.md", "src/app.py"]}, True),
        ({"file_path": "src/app.py"}, False),
        ({"file_path": "docs/requirements/notes.txt"}, False),
        ({}, False),
    ],
)
def test_requirements_nudge_hook_is_print_only_and_advisory(tool_input: dict, expect_nudge: bool) -> None:
    """The hook prints a nudge for requirements edits, stays silent otherwise,
    and ALWAYS exits 0 (advisory — never blocks, never runs pipelines)."""
    payload = json.loads(NUDGE_RECIPE.read_text(encoding="utf-8"))
    command = payload["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert command.startswith('python -c "') and command.endswith('"')
    python_code = command[len('python -c "'):-1].replace('\\"', '"')

    result = subprocess.run(
        ["python3", "-c", python_code],
        env={**os.environ, "TOOL_INPUT": json.dumps(tool_input)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    if expect_nudge:
        assert "requirements changed" in result.stdout
        assert "codd greenfield --resume" in result.stdout
    else:
        assert result.stdout.strip() == ""


# ═══════════════════════════════════════════════════════════
# D. README sync + examples index
# ═══════════════════════════════════════════════════════════

def test_examples_readme_indexes_all_vehicles() -> None:
    body = EXAMPLES_README.read_text(encoding="utf-8")
    assert "greenfield_autopilot.sh" in body
    assert "claude_workflows/codd-greenfield.js" in body
    assert "codd skills install codd-greenfield" in body
    assert "codd greenfield --requirements" in body


@pytest.mark.parametrize("readme", ["README.md", "README_ja.md", "README_zh.md"])
def test_readmes_feature_the_greenfield_autopilot_and_vehicles(readme: str) -> None:
    body = (REPO_ROOT / readme).read_text(encoding="utf-8")
    assert "codd greenfield --requirements docs/requirements/requirements.md" in body
    assert "examples/greenfield_autopilot.sh" in body
    assert "examples/claude_workflows/codd-greenfield.js" in body
    assert "codd skills install codd-greenfield --target both" in body
    assert "codd greenfield --requirements FILE" in body  # core-commands table row
    assert "claude_requirements_nudge.json" in body  # hook integration section
