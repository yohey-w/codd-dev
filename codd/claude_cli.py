"""Claude CLI command normalization helpers."""

from __future__ import annotations

import os
from pathlib import Path
import shlex

DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
#: Reasoning effort injected when the caller does not specify one.
#:
#: NOT ``max``. CoDD's AI stages (generate / implement / extract) run long,
#: noisy, tool-driven contexts, which is exactly the regime where a larger
#: reasoning budget is not a strict upgrade — accuracy can DECREASE with more
#: test-time compute on distractor-heavy tasks, and lower "overthinking"
#: correlates with better agentic outcomes. ``max`` remains the right choice
#: for a narrow, closed problem and is honoured whenever the caller passes it
#: explicitly (see ``_ensure_option``: an existing ``--effort`` always wins).
DEFAULT_CLAUDE_EFFORT = "xhigh"


def with_default_claude_permission_bypass(command: list[str]) -> list[str]:
    """Add Claude defaults and isolate explicitly tool-less calls from MCP."""
    if not command or not _is_claude_command(command[0]):
        return command

    prepared = list(command)
    tool_arguments = _tool_arguments(prepared, {"--tools"})
    if (
        any(values for _, values in tool_arguments)
        and not any(_tool_names(values) for _, values in tool_arguments)
        and not _has_mcp_option(prepared)
    ):
        # --tools is variadic: append neither to its values nor after stdin '-'.
        prepared.insert(tool_arguments[0][0], "--strict-mcp-config")
    _ensure_option(prepared, "--model", DEFAULT_CLAUDE_MODEL)
    _ensure_option(prepared, "--effort", DEFAULT_CLAUDE_EFFORT)

    if not _safe_permissions_requested():
        _ensure_option(prepared, "--permission-mode", "bypassPermissions", replace=True)
        if "--dangerously-skip-permissions" not in prepared:
            prepared.append("--dangerously-skip-permissions")
    return prepared


def _is_claude_command(executable: str) -> bool:
    name = Path(executable).name.lower()
    return name == "claude" or name.startswith("claude.")


def _tool_arguments(
    command: list[str], flags: set[str],
) -> list[tuple[int, list[str]]]:
    """Collect variadic tool values, retaining explicit empty versus bare flags."""
    arguments: list[tuple[int, list[str]]] = []
    for index, part in enumerate(command):
        flag, separator, inline = part.partition("=")
        if flag not in flags:
            continue
        values = [inline] if separator else []
        for value in command[index + 1:]:
            if value.startswith("-"):
                break
            values.append(value)
        arguments.append((index, values))
    return arguments


def _tool_names(values: list[str]) -> set[str]:
    return {name for value in values for name in value.replace(",", " ").split()}


def ai_command_has_tools(ai_command: str) -> bool:
    """Whether an explicit tool/allowed-tools list grants any tools.

    Missing, bare, empty, or unparseable lists fail closed to context-only.
    Accept comma and whitespace separated names; options and stdin '-' end a list.
    """
    try:
        command = shlex.split(ai_command)
    except ValueError:
        return False
    return any(
        _tool_names(values)
        for _, values in _tool_arguments(command, {"--tools", "--allowedTools", "--allowed-tools"})
    )


def _has_mcp_option(command: list[str]) -> bool:
    return any(
        part.partition("=")[0] in {"--mcp-config", "--strict-mcp-config"}
        for part in command
    )


def claude_command_loads_all_mcp_definitions(ai_command: str) -> bool:
    """Detect tool-enabled Claude commands without MCP isolation or ToolSearch.

    ``default`` names the CLI's built-in tool set, which carries ToolSearch, so
    it keeps MCP tool definitions deferred and is not reported.
    """
    try:
        command = shlex.split(ai_command)
    except ValueError:
        return False
    if not command or not _is_claude_command(command[0]) or _has_mcp_option(command):
        return False
    tools = set().union(*(
        _tool_names(values) for _, values in _tool_arguments(command, {"--tools"})
    ))
    return bool(tools) and not tools & {"ToolSearch", "default"}


def _ensure_option(command: list[str], option: str, value: str, *, replace: bool = False) -> None:
    for index, part in enumerate(command):
        if part == option:
            if replace and index + 1 < len(command):
                command[index + 1] = value
            return
        if part.startswith(f"{option}="):
            if replace:
                command[index] = f"{option}={value}"
            return

    command.extend([option, value])


def _safe_permissions_requested() -> bool:
    value = os.environ.get("CODD_CLAUDE_SAFE_PERMISSIONS", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}
