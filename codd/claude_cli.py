"""Claude CLI command normalization helpers."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
DEFAULT_CLAUDE_EFFORT = "max"


def with_default_claude_permission_bypass(command: list[str]) -> list[str]:
    """Add CoDD's default Claude model, effort, and permission mode."""
    if not command or not _is_claude_command(command[0]):
        return command

    prepared = list(command)
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
