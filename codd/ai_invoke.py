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

Recoverable-error auto-recovery (CLI-agnostic, active at the shared chokepoint
regardless of the caller's ``retries`` setting, so implement / generate /
greenfield all benefit): a single AI call that aborts with a clearly-recoverable
CLI error is recovered without human intervention instead of failing the task.
``_is_transient_error`` (socket/connection drop, timeout, 429/5xx gateway,
overload) triggers a bounded auto-retry with backoff; ``_is_output_ceiling_error``
(the per-call output-token maximum) triggers ONE re-issue of the same call with a
raised ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` budget — the documented lever, threaded
as a per-call subprocess env (or raised on ``os.environ`` for the adapter path).
CLIs that ignore the env var simply get one extra identical attempt. Both
classifiers are conservative: permanent auth/billing/validation/missing-binary
errors never match and surface immediately. Each recovery logs a one-line
``[codd]`` notice on stderr so it is visible in greenfield stage output.

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
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from codd.claude_cli import with_default_claude_permission_bypass
from codd.defaults import AI_TIMEOUT_SECONDS as _DEFAULT_AI_TIMEOUT_SECONDS

# Default AI command (moved from codd.generator; generator re-exports it).
DEFAULT_AI_COMMAND = (
    'claude --print --permission-mode bypassPermissions '
    '--dangerously-skip-permissions --model claude-opus-4-8 --effort max --tools ""'
)

# ── Wall-clock timeout on the AI subprocess (anti silent-hang) ──────────────
#
# A model CLI can stall a single call indefinitely with NO error and NO output
# (process Sl, ~0 CPU, blocked on a half-open socket) — the recoverable-error
# classifier never fires because nothing is ever returned to classify, so the
# whole pipeline freezes forever. Every AI subprocess call therefore carries a
# finite wall-clock timeout; ``subprocess.run(..., timeout=...)`` kills the
# child it spawned before raising ``TimeoutExpired`` (CPython sends SIGKILL and
# reaps it), so a timed-out call leaves no zombie/orphan. A timeout is treated
# as a transient transport failure and flows into the SAME bounded auto-retry /
# backoff as a dropped socket (see ``_invoke_with_recovery``): a one-off network
# stall self-recovers, a persistently-hung call fails loudly after the bounded
# attempts instead of hanging.
#
# Default is the shared SSoT ``codd.defaults.AI_TIMEOUT_SECONDS`` (3600s) so the
# direct-subprocess path, the file-writing-agent path, and the deployment
# adapter (``SubprocessAiCommand``, which already times out via the same SSoT)
# all agree. 3600s is generous: heavy reasoning calls (large design docs on a
# max-effort model) legitimately run many minutes; the longest legitimate single
# call observed is ~20min, and the silent stall that motivated this guard ran
# 47min+ and climbing — well clear of any real call. Operators who want a
# tighter bound override per-call WITHOUT lowering the global default via, in
# precedence order:
#   1. ``CODD_AI_CALL_TIMEOUT`` env var (this guard's dedicated knob)
#   2. ``CODD_AI_TIMEOUT_SECONDS`` env var (the shared AI-timeout SSoT)
#   3. ``ai.call_timeout_seconds`` in codd.yaml (this guard's dedicated key)
#   4. ``llm.timeout_seconds`` in codd.yaml (the shared AI-timeout SSoT)
#   5. the ``AI_TIMEOUT_SECONDS`` default.
# Each retry attempt gets its own fresh timeout (it is applied per subprocess
# call, not across the whole recovery loop).
DEFAULT_AI_CALL_TIMEOUT_SECONDS: float = float(_DEFAULT_AI_TIMEOUT_SECONDS)
#: Dedicated env override for the per-call wall-clock timeout (highest priority).
AI_CALL_TIMEOUT_ENV = "CODD_AI_CALL_TIMEOUT"
#: Shared AI-timeout env override (honored as a fallback so all call sites agree).
AI_TIMEOUT_ENV = "CODD_AI_TIMEOUT_SECONDS"

# Plain text-in/text-out AI invoker signature shared across the codebase.
AiInvoke = Callable[[str], str]

# The implement/generate file-output contract a text-in/text-out CLI emits on
# stdout: one ``=== FILE: <path> ===`` header per generated file. Kept local
# (not imported from ``codd.implementer``) to avoid a circular import — the
# marker shape is a stable cross-module contract.
_FILE_BLOCK_MARKER = re.compile(r"^=== FILE: .+? ===\s*$", re.MULTILINE)


def _stdout_carries_file_contract(stdout: str) -> bool:
    """True when *stdout* already contains the ``=== FILE: ===`` output contract.

    A CLI that honours the prompt's stdout contract (emit ``=== FILE:`` blocks,
    write nothing to disk) is just as valid as one that writes files directly;
    both produce output CoDD's file-block parser consumes. Detecting the
    contract on stdout lets the file-writing-agent path stay CLI-agnostic
    instead of *requiring* on-disk writes.
    """
    return bool(stdout and _FILE_BLOCK_MARKER.search(stdout))

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


def _coerce_positive_float(value: Any) -> float | None:
    """Parse *value* into a positive float, or ``None`` if it isn't one.

    Used to read the timeout from env strings and config scalars defensively: a
    blank, unparseable, zero, or negative value is ignored (falls through to the
    next override source) rather than turning the timeout off.
    """
    if value is None:
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _config_nested_value(config: Mapping[str, Any] | None, *path: str) -> Any:
    value: Any = config
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def resolve_ai_call_timeout(
    config: Mapping[str, Any] | None = None,
    override: float | None = None,
) -> float:
    """Resolve the per-call wall-clock timeout (seconds) for an AI subprocess.

    Precedence (first positive value wins): explicit *override* >
    ``CODD_AI_CALL_TIMEOUT`` env > ``CODD_AI_TIMEOUT_SECONDS`` env >
    ``ai.call_timeout_seconds`` config > ``llm.timeout_seconds`` config >
    :data:`DEFAULT_AI_CALL_TIMEOUT_SECONDS`. The two env vars and two config
    keys keep this guard's dedicated knob (``ai.call_timeout_seconds`` /
    ``CODD_AI_CALL_TIMEOUT``) layered on top of the shared AI-timeout SSoT
    (``llm.timeout_seconds`` / ``CODD_AI_TIMEOUT_SECONDS``) so every AI call
    site agrees by default. Always finite and positive.
    """
    candidates = (
        override,
        os.environ.get(AI_CALL_TIMEOUT_ENV),
        os.environ.get(AI_TIMEOUT_ENV),
        _config_nested_value(config, "ai", "call_timeout_seconds"),
        _config_nested_value(config, "llm", "timeout_seconds"),
    )
    for candidate in candidates:
        resolved = _coerce_positive_float(candidate)
        if resolved is not None:
            return resolved
    return DEFAULT_AI_CALL_TIMEOUT_SECONDS


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
    *, env: Mapping[str, str] | None = None, timeout: float | None = None,
) -> str:
    """Invoke an AI agent that writes files directly, capture changes as === FILE: === blocks.

    *env*, when given, replaces the child process environment (used by the
    output-ceiling recovery to raise ``CLAUDE_CODE_MAX_OUTPUT_TOKENS``).
    *timeout* is the per-call wall-clock budget (seconds); ``None`` resolves the
    shared default via :func:`resolve_ai_call_timeout`. A timed-out child is
    killed by ``subprocess.run`` before the failure is raised (no zombie), and
    the failure is raised with the ``AI command failed:`` prefix so it routes
    into the same transient auto-retry as a dropped socket.
    """
    cwd = str(project_root)
    call_timeout = resolve_ai_call_timeout() if timeout is None else timeout

    # Stage all current state as baseline for change detection
    subprocess.run(["git", "add", "-A"], cwd=cwd, capture_output=True)

    try:
        result = subprocess.run(
            command, input=prompt, capture_output=True, text=True, encoding="utf-8",
            check=False, cwd=cwd, timeout=call_timeout,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"AI command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired:
        # subprocess.run already SIGKILLed and reaped the child before raising.
        # Surface as a transient failure so the recovery wrapper retries it.
        raise ValueError(_AI_CALL_TIMEOUT_MESSAGE.format(seconds=_format_seconds(call_timeout)))

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
        # CLI-agnostic fallback: an agent classified as "file-writing" may still
        # honour the prompt's stdout contract (emit ``=== FILE: ===`` blocks,
        # write nothing to disk) — e.g. ``codex exec`` under a read-only or
        # text-out invocation. That stdout is exactly what CoDD's file-block
        # parser consumes, so use it directly instead of failing on the absence
        # of on-disk writes. Restore the staged baseline first so the working
        # tree is left untouched.
        if _stdout_carries_file_contract(result.stdout):
            subprocess.run(["git", "reset", "--quiet"], cwd=cwd, capture_output=True)
            return result.stdout
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


#: Message raised on a wall-clock timeout. Carries the ``AI command failed:``
#: prefix so it matches ``_is_transient_failure`` AND the "timed out" /
#: "timeout" transient patterns — a timed-out call is routed into the SAME
#: bounded auto-retry/backoff as a dropped socket, no parallel path.
_AI_CALL_TIMEOUT_MESSAGE = "AI command failed: AI call timed out after {seconds}s"


def _format_seconds(seconds: float) -> str:
    """Render a timeout for messages: drop a redundant ``.0`` on whole numbers."""
    return f"{int(seconds)}" if float(seconds).is_integer() else f"{seconds:g}"


def _invoke_subprocess(
    ai_command: str, prompt: str, *, project_root: Path | None = None,
    env: Mapping[str, str] | None = None, timeout: float | None = None,
) -> str:
    """Single-attempt subprocess invocation (the historical generator path).

    *env*, when given, replaces the child process environment — used by the
    output-ceiling recovery to re-issue the same call with a raised
    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` budget. *timeout* is the per-call
    wall-clock budget (seconds); ``None`` resolves the shared default via
    :func:`resolve_ai_call_timeout`. ``subprocess.run`` kills the child it
    spawned on timeout (no zombie/orphan) before this re-raises the stall as a
    transient ``AI command failed: AI call timed out ...`` so it flows into the
    same auto-retry as a dropped socket.
    """
    command = with_default_claude_permission_bypass(shlex.split(ai_command))
    if not command:
        raise ValueError("ai_command must not be empty")

    call_timeout = resolve_ai_call_timeout() if timeout is None else timeout

    if is_file_writing_agent(command) and project_root is not None:
        # Pass env/timeout only when an override is present so existing
        # monkeypatched fakes with the historical (command, prompt,
        # project_root) signature keep working (the common path passes env=None
        # and timeout=None, and invoke_file_writing_agent self-resolves the same
        # shared timeout default via resolve_ai_call_timeout()).
        if env is not None or timeout is not None:
            return invoke_file_writing_agent(
                command, prompt, project_root, env=env, timeout=call_timeout
            )
        return invoke_file_writing_agent(command, prompt, project_root)

    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True, encoding="utf-8",
            check=False,
            timeout=call_timeout,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"AI command not found: {command[0]}") from exc
    except subprocess.TimeoutExpired:
        # subprocess.run already SIGKILLed and reaped the child before raising.
        # Surface as a transient failure so the recovery wrapper retries it.
        raise ValueError(_AI_CALL_TIMEOUT_MESSAGE.format(seconds=_format_seconds(call_timeout)))

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


# ── Recoverable-error classification (CLI-agnostic) ─────────────────────────
#
# A model CLI can abort a single call for reasons that are NOT the prompt's
# fault and that a re-run (or a re-run with a raised output budget) clears
# without human intervention. We classify those conservatively from the CLI's
# stderr/exit text so the invocation layer can auto-recover instead of failing
# the whole task. The patterns are pattern-based and deliberately narrow — they
# must match clearly-transient/recoverable conditions across CLIs (Claude,
# Codex, and any other text-in/text-out CLI) and NEVER match a permanent
# auth/billing/validation error (mirroring the "AI command not found is
# permanent" rule: a permanent error must surface immediately, not loop).

# Transient transport: connection resets, dropped sockets, gateway hiccups,
# and timeouts. Re-issuing the identical call typically succeeds.
_TRANSIENT_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"socket connection was closed", re.IGNORECASE),
    re.compile(r"connection (?:closed|reset|aborted|refused)", re.IGNORECASE),
    re.compile(r"\bconnection error\b", re.IGNORECASE),
    re.compile(r"\bECONNRESET\b|\bECONNREFUSED\b|\bEPIPE\b|\bETIMEDOUT\b", re.IGNORECASE),
    re.compile(r"\b(?:read|request|connection|socket) ?timed? ?out\b", re.IGNORECASE),
    re.compile(r"\btimeout\b", re.IGNORECASE),
    # Bare "timed out" (no transport-word prefix) — our own wall-clock-timeout
    # message ("AI call timed out after Ns") and any CLI that reports a plain
    # stall. Conservative: permanent auth/billing/validation errors never say
    # "timed out", so this routes a hung call into the same transient retry.
    re.compile(r"\btimed out\b", re.IGNORECASE),
    re.compile(r"\b(?:429|500|502|503|504)\b", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"(?:service|server|gateway) (?:unavailable|error)", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"\bEOF\b|unexpected(?:ly)? (?:closed|disconnect)", re.IGNORECASE),
)

# Output ceiling: the model hit its per-call output-token maximum. The
# documented lever is the CLAUDE_CODE_MAX_OUTPUT_TOKENS env var; raising it and
# re-issuing the SAME call is the CLI-agnostic recovery (a CLI that ignores the
# env var simply gets one extra identical attempt — no harm).
_OUTPUT_CEILING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"output token (?:maximum|limit)", re.IGNORECASE),
    re.compile(r"max(?:imum)?[ _]output[ _]tokens", re.IGNORECASE),
    re.compile(r"exceeded the \d+ output token", re.IGNORECASE),
    re.compile(r"response exceeded.*output token", re.IGNORECASE),
    re.compile(r"\bCLAUDE_CODE_MAX_OUTPUT_TOKENS\b", re.IGNORECASE),
)

#: Env var the output-ceiling recovery raises. CLI-agnostic: CLIs that honour it
#: get a bigger budget; CLIs that don't are unaffected.
OUTPUT_TOKENS_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
#: Budget the recovery sets when the ceiling is hit and the env is unset/low.
RAISED_OUTPUT_TOKENS = 64000
#: Bounded transient-transport auto-retries (in addition to any caller retries).
TRANSIENT_AUTO_RETRIES = 3
#: Short backoff (seconds) between transient auto-retries; index-scaled.
TRANSIENT_BACKOFF_SECONDS = 1.5


def _error_text(exc: ValueError) -> str:
    """The CLI's stderr/stdout detail carried inside an ``AI command failed:``.

    ``_invoke_subprocess`` raises ``AI command failed: <detail>`` where
    *detail* is the CLI's stderr (or stdout). For classification we want that
    detail, but matching the whole message is harmless too.
    """
    return str(exc)


def _is_transient_error(stderr: str) -> bool:
    """True when *stderr* clearly describes a transient/recoverable transport
    failure (socket/connection drop, timeout, 429/5xx gateway, overload).

    Conservative by construction: only re-runnable conditions match. Auth,
    billing, quota-exhausted, and validation errors do NOT match and stay
    permanent.
    """
    if not stderr:
        return False
    return any(pattern.search(stderr) for pattern in _TRANSIENT_ERROR_PATTERNS)


def _is_output_ceiling_error(stderr: str) -> bool:
    """True when *stderr* describes hitting the per-call output-token ceiling."""
    if not stderr:
        return False
    return any(pattern.search(stderr) for pattern in _OUTPUT_CEILING_PATTERNS)


def _raised_output_budget_env() -> dict[str, str] | None:
    """A subprocess env with ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` raised.

    Returns ``None`` (signalling "do not retry — the budget is already high")
    when the current env already sets it at or above the raised value, so a
    genuine ceiling at a deliberately-high budget surfaces honestly instead of
    looping. Otherwise returns ``os.environ`` plus the raised value.
    """
    current = os.environ.get(OUTPUT_TOKENS_ENV)
    if current is not None:
        try:
            if int(current.strip()) >= RAISED_OUTPUT_TOKENS:
                return None
        except ValueError:
            pass  # unparseable current value: treat as low, raise it.
    env = dict(os.environ)
    env[OUTPUT_TOKENS_ENV] = str(RAISED_OUTPUT_TOKENS)
    return env


def _invoke_with_recovery(
    attempt: Callable[..., str],
    prompt: str,
    *,
    adapter_routed: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Run one logical AI call with auto-recovery for recoverable errors.

    Wraps a single :func:`invoke_ai` attempt (one prompt, no feedback rewrite)
    with two CLI-agnostic recoveries that need NO human intervention and are
    active regardless of the caller's ``retries`` setting (so implement /
    generate / greenfield all benefit at their shared chokepoint):

    1. **Transient transport** (``_is_transient_error``): socket/connection
       drop, timeout, 429/5xx gateway, overload — bounded auto-retry with a
       short backoff (:data:`TRANSIENT_AUTO_RETRIES` attempts).
    2. **Output ceiling** (``_is_output_ceiling_error``): re-issue the SAME
       call ONCE with a raised ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` budget (the
       documented lever; CLIs that ignore it are unaffected). For the
       subprocess path the budget is threaded as a per-call env; for the
       adapter path (which owns its own env) the budget is raised on
       ``os.environ`` only for the retried call and restored afterwards.

    Permanent errors (missing binary, auth/billing/validation) are NOT matched
    by either classifier and propagate immediately. A recovery emits a
    one-line ``[codd]`` notice on stderr so it is visible in greenfield stage
    output.
    """
    ceiling_recovered = False
    transient_attempts = 0
    while True:
        try:
            return attempt(prompt)
        except ValueError as exc:
            detail = _error_text(exc)
            # Output ceiling: raise the budget and retry the SAME call once.
            if not ceiling_recovered and _is_output_ceiling_error(detail):
                raised_env = _raised_output_budget_env()
                if raised_env is None:
                    raise  # budget already high — surface honestly.
                ceiling_recovered = True
                print(
                    f"[codd] AI hit the output-token ceiling; retrying once with "
                    f"{OUTPUT_TOKENS_ENV}={RAISED_OUTPUT_TOKENS}.",
                    file=sys.stderr,
                )
                if adapter_routed:
                    prior = os.environ.get(OUTPUT_TOKENS_ENV)
                    os.environ[OUTPUT_TOKENS_ENV] = str(RAISED_OUTPUT_TOKENS)
                    try:
                        return attempt(prompt)
                    finally:
                        if prior is None:
                            os.environ.pop(OUTPUT_TOKENS_ENV, None)
                        else:
                            os.environ[OUTPUT_TOKENS_ENV] = prior
                else:
                    return attempt(prompt, raised_env)
            # Transient transport: bounded auto-retry with backoff.
            if transient_attempts < TRANSIENT_AUTO_RETRIES and _is_transient_error(detail):
                transient_attempts += 1
                print(
                    f"[codd] transient AI transport error "
                    f"(auto-retry {transient_attempts}/{TRANSIENT_AUTO_RETRIES}): "
                    f"{detail[:200]}",
                    file=sys.stderr,
                )
                sleep(TRANSIENT_BACKOFF_SECONDS * transient_attempts)
                continue
            raise


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

    Wall-clock timeout: every direct-subprocess AI call carries a finite
    per-call timeout (resolved via :func:`resolve_ai_call_timeout`; default
    :data:`DEFAULT_AI_CALL_TIMEOUT_SECONDS`). A stall that exceeds it kills the
    child and raises a transient ``AI call timed out ...`` that flows into the
    same bounded auto-retry as a dropped socket, so a silent no-output hang can
    no longer freeze the pipeline forever. Each retry attempt gets a fresh
    timeout. The deployment-adapter path owns its own timeout (the adapter
    already applies ``resolve_timeout`` from the shared SSoT).
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

            # The deployment adapter owns its own subprocess env, so the
            # output-ceiling env-raise cannot be threaded per-call. Recovery
            # falls back to raising the budget on os.environ for the duration of
            # the retried adapter call (restored in ``finally``). Transient
            # auto-retry still applies via the same identical re-invocation.
            def attempt(current_prompt: str, env: Mapping[str, str] | None = None) -> str:
                return adapter.invoke(current_prompt)
        else:
            # Hardened calls are plain text-in/text-out by definition: never
            # route them through the file-writing-agent capture machinery.
            subprocess_root = None if harden_read_only else project_root

            def attempt(current_prompt: str, env: Mapping[str, str] | None = None) -> str:
                # timeout is left to _invoke_subprocess to resolve (via
                # resolve_ai_call_timeout) so the historical call sites/fakes
                # with the (command, prompt, project_root[, env]) shape keep
                # working; the resolver still honours the env vars and config.
                return _invoke_subprocess(
                    command_str, current_prompt, project_root=subprocess_root, env=env
                )

        current_prompt = prompt
        max_attempts = max(0, retries) + 1
        for attempt_index in range(max_attempts):
            try:
                return _invoke_with_recovery(attempt, current_prompt, adapter_routed=config is not None)
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
    "AI_CALL_TIMEOUT_ENV",
    "AI_TIMEOUT_ENV",
    "DEFAULT_AI_CALL_TIMEOUT_SECONDS",
    "DEFAULT_AI_COMMAND",
    "RetryFeedback",
    "force_claude_print",
    "invoke_ai",
    "invoke_file_writing_agent",
    "is_codex_exec_command",
    "is_file_writing_agent",
    "prepare_read_only_codex",
    "resolve_ai_call_timeout",
    "resolve_ai_command",
]


# Re-exported for tests/diagnostics: the stdout-contract detector used by the
# file-writing-agent fallback (kept out of the public API surface above).
__all__.append("_stdout_carries_file_contract")

# Re-exported for tests/diagnostics: the recoverable-error classifiers and the
# recovery wrapper used by ``invoke_ai`` (kept out of the public API surface).
__all__ += [
    "OUTPUT_TOKENS_ENV",
    "RAISED_OUTPUT_TOKENS",
    "TRANSIENT_AUTO_RETRIES",
    "_is_output_ceiling_error",
    "_is_transient_error",
]
