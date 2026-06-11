"""Unified AI CLI resolution and invocation layer (RF4).

Before this module existed, at least five entry points resolved/invoked the
configured ``ai_command`` with divergent capabilities (generator had retry +
file-writing-agent routing; extract_ai had neither; fix/phenomenon hardened
``codex exec`` to read-only; the deployment factory added the Codex App Server
transport; llm/design_doc_extractor re-implemented resolution). This module is
now the single source of truth for:

- ``resolve_ai_command()`` — override > ``ai_commands.<name>`` > ``ai_command``
  > default precedence (moved from ``codd.generator``).
- ``invoke_ai()`` — one invocation entry with Claude permission bypass,
  file-writing-agent routing, bounded retries on transient failures, optional
  read-only hardening for ``codex exec``, optional ``--print`` forcing for
  Claude, and optional routing through the deployment adapter
  (``get_ai_command``) so Codex App Server transport selection is preserved.

Backward compatibility: ``codd.generator`` re-exports ``_resolve_ai_command``
and ``_invoke_ai_command`` as aliases of these functions, so the many modules
(and test monkeypatches) that import those names keep working unchanged.
Likewise ``codd.fix.phenomenon_fixer`` re-exports
``_prepare_plain_text_ai_command`` / ``_is_codex_exec_command``.

Error contract: every failure path raises ``ValueError`` with the exact
messages callers already catch ("AI command not found: ...", "AI command
failed: ...", "AI command returned empty output", "ai_command must not be
empty"). Adapter-routed invocations propagate the adapter's own exceptions
unchanged (e.g. ``AiCommandError``), exactly as the pre-RF4 builders did.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from codd.claude_cli import with_default_claude_permission_bypass

# Default AI command (moved from codd.generator; generator re-exports it).
DEFAULT_AI_COMMAND = (
    'claude --print --permission-mode bypassPermissions '
    '--dangerously-skip-permissions --model claude-opus-4-8 --effort max --tools ""'
)

# Plain text-in/text-out AI invoker signature shared across the codebase.
AiInvoke = Callable[[str], str]

# Retry feedback hook: (previous_prompt, error, attempt_index) -> next prompt.
RetryFeedback = Callable[[str, ValueError, int], str]


# ═══════════════════════════════════════════════════════════
# Resolution
# ═══════════════════════════════════════════════════════════

def resolve_ai_command(
    config: Mapping[str, Any],
    override: str | None = None,
    command_name: str | None = None,
    *,
    default: str = DEFAULT_AI_COMMAND,
) -> str:
    """Resolve the AI CLI command string.

    Precedence: explicit *override* > ``ai_commands.<command_name>`` (string or
    ``{command: ...}`` mapping) > top-level ``ai_command`` > *default*.
    """
    if override is not None:
        raw_command = override
    elif command_name and isinstance(config.get("ai_commands"), dict):
        command_value = config["ai_commands"].get(command_name)
        if isinstance(command_value, dict):
            raw_command = command_value.get("command") or config.get("ai_command", default)
        elif command_value is None:
            raw_command = config.get("ai_command", default)
        else:
            raw_command = command_value
    else:
        raw_command = config.get("ai_command", default)
    if not isinstance(raw_command, str) or not raw_command.strip():
        raise ValueError("ai_command must be a non-empty string")
    return raw_command.strip()


# ═══════════════════════════════════════════════════════════
# Command preparation helpers
# ═══════════════════════════════════════════════════════════

def is_file_writing_agent(command: list[str]) -> bool:
    """Detect AI agents that write output to filesystem instead of stdout.

    Codex always writes to filesystem.
    Claude without -p/--print runs in interactive mode (file-writing).
    Claude with -p/--print outputs to stdout.
    """
    if not command:
        return False
    if "codex" in command[0]:
        return True
    if "claude" in command[0]:
        return "-p" not in command and "--print" not in command
    return False


def force_claude_print(command: str) -> str:
    """Append ``--print`` to a Claude CLI command that lacks -p/--print.

    Non-Claude commands are returned unchanged. Used by entry points that need
    plain text-in/text-out (otherwise Claude runs interactively).
    """
    parts = shlex.split(command)
    if parts and "claude" in parts[0].lower() and "-p" not in parts and "--print" not in parts:
        parts.append("--print")
        return shlex.join(parts)
    return command


def prepare_read_only_codex(command: str, safe_root: Path) -> str:
    """Harden agentic AI CLIs so plain-text LLM calls cannot edit the project.

    ``codex exec`` invocations get their dangerous flags stripped and are
    forced into ``--sandbox read-only`` from an empty workspace *safe_root*.
    Non-codex commands are returned unchanged.
    """

    parts = shlex.split(command)
    if not is_codex_exec_command(parts):
        return command

    prompt_arg: str | None = None
    if parts and parts[-1] == "-":
        prompt_arg = parts.pop()

    cleaned: list[str] = []
    skip_next = False
    flags_with_value = {"--sandbox", "-s", "--cd", "-C", "--add-dir"}
    banned_flags = {
        "--full-auto",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
    }
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if part in banned_flags:
            continue
        if part in flags_with_value:
            skip_next = True
            continue
        if part.startswith("--sandbox=") or part.startswith("--cd=") or part.startswith("--add-dir="):
            continue
        cleaned.append(part)

    cleaned.extend([
        "--sandbox",
        "read-only",
        "--cd",
        safe_root.as_posix(),
        "--skip-git-repo-check",
        "--ephemeral",
    ])
    if prompt_arg is not None:
        cleaned.append(prompt_arg)
    return shlex.join(cleaned)


def is_codex_exec_command(parts: list[str]) -> bool:
    """True when *parts* is a ``codex exec ...`` command line."""
    if len(parts) < 2:
        return False
    binary = os.path.basename(parts[0]).lower()
    return "codex" in binary and parts[1] == "exec"


# ═══════════════════════════════════════════════════════════
# Invocation
# ═══════════════════════════════════════════════════════════

def invoke_file_writing_agent(
    command: list[str], prompt: str, project_root: Path,
) -> str:
    """Invoke an AI agent that writes files directly, capture changes as === FILE: === blocks."""
    cwd = str(project_root)

    # Stage all current state as baseline for change detection
    subprocess.run(["git", "add", "-A"], cwd=cwd, capture_output=True)

    try:
        result = subprocess.run(
            command, input=prompt, capture_output=True, text=True, encoding="utf-8",
            check=False, cwd=cwd, timeout=3600,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"AI command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired:
        raise ValueError("AI command timed out (3600s)")

    print(
        f"[codd] file-writing agent finished: returncode={result.returncode} "
        f"stdout={len(result.stdout)}B stderr={len(result.stderr)}B cwd={cwd}",
        file=sys.stderr,
    )

    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()
                  or f"exit code {result.returncode}")
        raise ValueError(f"AI command failed: {detail}")

    # Detect files changed by the agent (unstaged vs index = agent's work)
    diff_out = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()
    untracked_out = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()

    changed_files = diff_out.splitlines() if diff_out else []
    new_files = untracked_out.splitlines() if untracked_out else []
    all_files = sorted(set(changed_files + new_files))

    if not all_files:
        raise ValueError("AI command did not produce any file changes")

    # Read changed files and format as CoDD file blocks
    parts: list[str] = []
    for rel_path in all_files:
        full_path = project_root / rel_path
        if full_path.is_file():
            content = full_path.read_text(encoding="utf-8", errors="replace")
            parts.append(f"=== FILE: {rel_path} ===")
            parts.append(content)
            parts.append("")

    if not parts:
        raise ValueError("AI command did not produce any readable file changes")

    # Revert: restore tracked files from index, remove agent-created files
    subprocess.run(["git", "checkout", "--", "."], cwd=cwd, capture_output=True)
    for f in new_files:
        fp = project_root / f
        if fp.is_file():
            fp.unlink()
    subprocess.run(["git", "reset", "--quiet"], cwd=cwd, capture_output=True)

    return "\n".join(parts)


def _invoke_subprocess(
    ai_command: str, prompt: str, *, project_root: Path | None = None,
) -> str:
    """Single-attempt subprocess invocation (the historical generator path)."""
    command = with_default_claude_permission_bypass(shlex.split(ai_command))
    if not command:
        raise ValueError("ai_command must not be empty")

    if is_file_writing_agent(command) and project_root is not None:
        return invoke_file_writing_agent(command, prompt, project_root)

    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True, encoding="utf-8",
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"AI command not found: {command[0]}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise ValueError(f"AI command failed: {detail}")

    if not result.stdout.strip():
        raise ValueError("AI command returned empty output")

    return result.stdout


def _is_transient_failure(exc: ValueError) -> bool:
    """Transient = nonzero exit or empty output. Missing binary is permanent."""
    message = str(exc)
    return message.startswith("AI command failed") or message == "AI command returned empty output"


def invoke_ai(
    ai_command: str,
    prompt: str,
    *,
    project_root: Path | None = None,
    retries: int = 0,
    retry_feedback: RetryFeedback | None = None,
    harden_read_only: bool = False,
    safe_root: Path | None = None,
    force_print_on_claude: bool = False,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Invoke the AI CLI through the one unified path.

    Args:
        ai_command: Resolved command string (see :func:`resolve_ai_command`).
        prompt: Prompt passed on stdin (or to the adapter).
        project_root: Enables file-writing-agent routing (codex / interactive
            claude write files; changes are captured as ``=== FILE: ===``
            blocks and reverted) on the subprocess path.
        retries: Bounded retries on *transient* failures (nonzero exit or
            empty output). "AI command not found" is permanent and never
            retried. ``retries=0`` reproduces single-attempt behavior.
        retry_feedback: Optional ``(prompt, error, attempt) -> next_prompt``
            hook to inject feedback into the retried prompt.
        harden_read_only: Harden ``codex exec`` commands to ``--sandbox
            read-only`` running from an empty workspace (*safe_root* or a
            per-call temporary directory).
        safe_root: Workspace used by ``harden_read_only`` (callers that keep a
            long-lived workspace pass it explicitly).
        force_print_on_claude: Append ``--print`` to Claude commands so output
            goes to stdout instead of an interactive session.
        config: When provided, route the invocation through the deployment
            adapter factory (``get_ai_command``) so the Codex App Server
            transport (and its fallback chain) is honored. ``None`` keeps the
            direct subprocess path.
    """
    command_str = ai_command

    held_workspace: tempfile.TemporaryDirectory | None = None
    effective_root = project_root
    try:
        if harden_read_only:
            workspace_root = safe_root
            if workspace_root is None:
                held_workspace = tempfile.TemporaryDirectory(prefix="codd-ai-")
                workspace_root = Path(held_workspace.name)
            command_str = prepare_read_only_codex(command_str, workspace_root)
            effective_root = workspace_root
        if force_print_on_claude:
            command_str = force_claude_print(command_str)

        if config is not None:
            from codd.deployment.providers.ai_command_factory import get_ai_command

            adapter = get_ai_command(config, project_root=effective_root, command_override=command_str)

            def attempt(current_prompt: str) -> str:
                return adapter.invoke(current_prompt)
        else:
            # Hardened calls are plain text-in/text-out by definition: never
            # route them through the file-writing-agent capture machinery.
            subprocess_root = None if harden_read_only else project_root

            def attempt(current_prompt: str) -> str:
                return _invoke_subprocess(command_str, current_prompt, project_root=subprocess_root)

        current_prompt = prompt
        max_attempts = max(0, retries) + 1
        for attempt_index in range(max_attempts):
            try:
                return attempt(current_prompt)
            except ValueError as exc:
                if attempt_index + 1 >= max_attempts or not _is_transient_failure(exc):
                    raise
                if retry_feedback is not None:
                    current_prompt = retry_feedback(current_prompt, exc, attempt_index)
        raise AssertionError("unreachable: retry loop always returns or raises")
    finally:
        if held_workspace is not None:
            held_workspace.cleanup()


__all__ = [
    "AiInvoke",
    "DEFAULT_AI_COMMAND",
    "RetryFeedback",
    "force_claude_print",
    "invoke_ai",
    "invoke_file_writing_agent",
    "is_codex_exec_command",
    "is_file_writing_agent",
    "prepare_read_only_codex",
    "resolve_ai_command",
]
