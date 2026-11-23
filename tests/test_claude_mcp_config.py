"""Claude MCP isolation, shared tool parsing, and advisory doctor coverage."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from click.testing import CliRunner
import pytest
import yaml

import codd.ai_invoke as ai_invoke
from codd.claude_cli import ai_command_has_tools, with_default_claude_permission_bypass
from codd.cli import _doctor_warnings, main
from codd.extract_ai import ai_command_has_tools as extract_ai_command_has_tools


@pytest.mark.parametrize("safe_permissions", [False, True])
@pytest.mark.parametrize("tools", [["--tools", ""], ["--tools="]])
@pytest.mark.parametrize("executable", ["claude", "/opt/bin/claude", "claude.exe"])
@pytest.mark.parametrize("trailing_stdin", [[], ["-"]])
def test_empty_tools_adds_strict_once_before_variadic_list(monkeypatch, safe_permissions, tools, executable, trailing_stdin):
    monkeypatch.setenv("CODD_CLAUDE_SAFE_PERMISSIONS", "1" if safe_permissions else "0")
    original = [executable, "--print", *tools, *trailing_stdin]

    command = with_default_claude_permission_bypass(original)

    assert command[:len(original) + 1] == [executable, "--print", "--strict-mcp-config", *tools, *trailing_stdin]
    assert command.count("--strict-mcp-config") == 1
    assert with_default_claude_permission_bypass(command) == command
    assert original == [executable, "--print", *tools, *trailing_stdin]
    assert ("--dangerously-skip-permissions" in command) is not safe_permissions
    assert command[command.index("--model") + 1] == "claude-opus-4-8"
    assert command[command.index("--effort") + 1] == "xhigh"


@pytest.mark.parametrize("mcp", [
    ["--mcp-config", "chosen.json"], ["--mcp-config=chosen.json"],
    ["--strict-mcp-config"], ["--strict-mcp-config=true"],
])
def test_explicit_mcp_options_keep_existing_normalization(monkeypatch, mcp):
    monkeypatch.delenv("CODD_CLAUDE_SAFE_PERMISSIONS", raising=False)
    original = ["claude", "--print", *mcp, "--tools", "", "-"]

    assert with_default_claude_permission_bypass(original) == original + [
        "--model", "claude-opus-4-8", "--effort", "xhigh",
        "--permission-mode", "bypassPermissions", "--dangerously-skip-permissions",
    ]


@pytest.mark.parametrize("arguments", [
    [], ["--tools"], ["--tools", "--print"], ["--tools", "-"],
    ["--tools", "Read,Grep"], ["--tools", "Read", "Grep", "-"],
    ["--tools=Read,Grep"], ["--allowedTools", ""],
])
def test_absent_bare_or_nonempty_tools_do_not_add_strict(arguments):
    command = with_default_claude_permission_bypass(["claude", "--print", *arguments])
    assert "--strict-mcp-config" not in command


@pytest.mark.parametrize("executable", ["codex", "other-ai", "claude-wrapper"])
def test_non_claude_empty_tools_is_unchanged(executable):
    command = [executable, "--print", "--tools", "", "-"]
    assert with_default_claude_permission_bypass(command) == command


@pytest.mark.parametrize("flag", ["--tools", "--allowedTools", "--allowed-tools"])
@pytest.mark.parametrize("value, expected", [
    ('"Read,Grep"', True), ('"Read Grep"', True), ("Read Grep", True),
    ('""', False), ("", False), ("--print", False), ("-", False),
])
def test_shared_tool_parser_handles_variadic_values_and_boundaries(flag, value, expected):
    command = f"claude --print {flag} {value}"
    assert ai_command_has_tools(command) is expected
    assert extract_ai_command_has_tools(command) is expected
    assert extract_ai_command_has_tools is ai_command_has_tools


@pytest.mark.parametrize("command, expected", [
    ("claude --tools=Read,Grep", True), ("claude --allowedTools=Read Grep", True),
    ("claude --allowed-tools=", False), ("claude --print", False),
    ('claude --tools "', False), ('claude --tools "" - prompt', False),
    ('claude --tools "" --model sonnet', False),
])
def test_shared_tool_parser_inline_values_and_invalid_commands(command, expected):
    assert extract_ai_command_has_tools(command) is expected


@pytest.mark.parametrize("command, expected", [
    # The list is variadic, exactly as the CLI parses it: every following word
    # that is not an option belongs to it, so a word after an empty value is a
    # tool name ...
    ('claude --tools "" Read', True), ("claude --tools= Read", True),
    ('claude --allowedTools "" Bash', True), ('claude --tools "" "" Read', True),
    # ... and an option, a bare "-" (stdin), or separators alone are not.
    ("claude --tools -", False), ("claude --tools --print", False),
    ('claude --tools ","', False), ("claude --tools ,", False),
    ("claude --tools -1", False), ('claude --tools "-x"', False),
    ("claude --tools -- Read", False),
])
def test_shared_tool_parser_follows_cli_variadic_semantics(command, expected):
    assert ai_command_has_tools(command) is expected


def _doctor_project(tmp_path: Path, **settings) -> Path:
    config_dir = tmp_path / "codd"
    config_dir.mkdir()
    (config_dir / "codd.yaml").write_text(yaml.safe_dump({
        "project": {"name": "sample", "language": "python"},
        **settings,
    }), encoding="utf-8")
    return tmp_path


def test_doctor_aggregates_unsafe_settings_into_one_advisory_and_exits_zero(tmp_path):
    project = _doctor_project(
        tmp_path,
        ai_command="claude --print --tools Read,Grep",
        ai_commands={
            "generate": "claude --print --tools Read Grep",
            "extract": {"command": "claude.exe --print --tools=Read,Grep"},
            "safe": 'claude --print --tools ""',
        },
    )

    warnings = [warning for warning in _doctor_warnings(project) if "MCP tool definitions" in warning]
    assert len(warnings) == 1
    assert "ai_command," in warnings[0]
    assert "ai_commands.generate" in warnings[0]
    assert "ai_commands.extract" in warnings[0]
    assert "ai_commands.safe" not in warnings[0]
    assert "load all MCP tool definitions" in warnings[0]
    assert "--strict-mcp-config" in warnings[0]
    assert "--mcp-config" in warnings[0]

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "CoDD doctor: WARN" in result.output
    assert result.output.count("MCP tool definitions") == 1


@pytest.mark.parametrize("command", [
    "claude --print --tools Read,Grep,ToolSearch",
    'claude --print --tools "Read Grep ToolSearch"',
    "claude --print --tools Read Grep ToolSearch",
    "claude --print --tools=Read,Grep,ToolSearch",
    "claude --print --tools Read,Grep --strict-mcp-config",
    "claude --print --tools Read,Grep --strict-mcp-config=true",
    "claude --print --tools Read,Grep --mcp-config chosen.json",
    "claude --print --tools Read,Grep --mcp-config=chosen.json",
    'claude --print --tools ""', "claude --print --tools=",
    "claude --print --tools", "claude --print",
    # "default" is the CLI's own default tool set and includes ToolSearch.
    "claude --print --tools default", "claude --print --tools=default",
    'other-ai --print --tools Read,Grep',
])
def test_doctor_does_not_warn_for_safe_or_inapplicable_commands(tmp_path, command):
    project = _doctor_project(tmp_path, ai_command=command, ai_commands={"extract": command})
    assert not any("MCP tool definitions" in warning for warning in _doctor_warnings(project))
    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "MCP tool definitions" not in result.output


@pytest.mark.parametrize("safe_permissions", [False, True])
@pytest.mark.parametrize("command_name", [None, "extract", "generate"])
def test_configured_ai_invocation_preserves_argv_and_prompt_stdin(monkeypatch, safe_permissions, command_name):
    monkeypatch.setenv("CODD_CLAUDE_SAFE_PERMISSIONS", "1" if safe_permissions else "0")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="result", stderr="")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)
    config = {
        "ai_command": 'claude --print --tools "" -',
        "ai_commands": {
            "extract": {"command": 'claude --print --tools= -'},
            "generate": 'claude --print --tools "" -',
        },
    }
    configured = ai_invoke.resolve_ai_command(config, command_name=command_name)
    assert ai_invoke.invoke_ai(configured, "the complete prompt") == "result"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    original = shlex.split(configured)
    assert argv[:len(original) + 1] == original[:2] + ["--strict-mcp-config"] + original[2:]
    assert kwargs["input"] == "the complete prompt"
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True


def test_default_ai_commands_are_strict_toolless_and_stdout_based():
    codd_dir = Path(ai_invoke.__file__).parent
    defaults = yaml.safe_load((codd_dir / "defaults.yaml").read_text(encoding="utf-8"))
    template = (codd_dir / "templates" / "codd.yaml.tmpl").read_text(encoding="utf-8")
    template_line = next(line for line in template.splitlines() if line.startswith("ai_command:"))
    template_command = yaml.safe_load(template_line)["ai_command"]
    assert ai_invoke.DEFAULT_AI_COMMAND == defaults["ai_command"] == template_command
    for command in [ai_invoke.DEFAULT_AI_COMMAND, defaults["ai_command"], template_command]:
        argv = shlex.split(command)
        assert argv.count("--strict-mcp-config") == 1
        assert argv[argv.index("--tools") + 1] == ""
        assert not ai_command_has_tools(command)
        assert not ai_invoke.is_file_writing_agent(argv)
