"""Tests for codd.ai_invoke — the unified AI resolution/invocation layer (RF4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import codd.ai_invoke as ai_invoke
from codd.ai_invoke import (
    DEFAULT_AI_COMMAND,
    force_claude_print,
    invoke_ai,
    invoke_file_writing_agent,
    is_codex_exec_command,
    is_file_writing_agent,
    prepare_read_only_codex,
    resolve_ai_command,
)


# ═══════════════════════════════════════════════════════════
# Resolution precedence
# ═══════════════════════════════════════════════════════════


def test_resolve_uses_default_when_no_config():
    assert resolve_ai_command({}, None) == DEFAULT_AI_COMMAND


def test_resolve_override_takes_precedence_over_everything():
    config = {
        "ai_command": "global-ai",
        "ai_commands": {"generate": "per-command-ai"},
    }
    assert resolve_ai_command(config, "cli-override", command_name="generate") == "cli-override"


def test_resolve_per_command_string_overrides_global():
    config = {
        "ai_command": "global-ai",
        "ai_commands": {"generate": "per-command-ai"},
    }
    assert resolve_ai_command(config, None, command_name="generate") == "per-command-ai"


def test_resolve_per_command_mapping_uses_command_key():
    config = {
        "ai_command": "global-ai",
        "ai_commands": {"generate": {"command": "mapped-ai --print"}},
    }
    assert resolve_ai_command(config, None, command_name="generate") == "mapped-ai --print"


def test_resolve_falls_back_to_global_when_command_not_listed():
    config = {
        "ai_command": "global-ai",
        "ai_commands": {"generate": "per-command-ai"},
    }
    assert resolve_ai_command(config, None, command_name="implement") == "global-ai"


def test_resolve_falls_back_to_default_when_no_ai_commands_dict():
    assert resolve_ai_command({}, None, command_name="generate") == DEFAULT_AI_COMMAND


def test_resolve_rejects_empty_string():
    with pytest.raises(ValueError, match="non-empty string"):
        resolve_ai_command({"ai_command": ""}, None)


def test_resolve_per_command_rejects_empty_string():
    config = {"ai_commands": {"generate": "   "}}
    with pytest.raises(ValueError, match="non-empty string"):
        resolve_ai_command(config, None, command_name="generate")


def test_resolve_custom_default_is_used():
    assert resolve_ai_command({}, None, default="ai") == "ai"


def test_resolve_strips_whitespace():
    assert resolve_ai_command({"ai_command": "  my-ai --print  "}, None) == "my-ai --print"


# ═══════════════════════════════════════════════════════════
# Helpers: file-writing detection / claude --print / codex hardening
# ═══════════════════════════════════════════════════════════


def test_is_file_writing_agent_truth_table():
    assert is_file_writing_agent(["codex", "exec"]) is True
    assert is_file_writing_agent(["claude"]) is True
    assert is_file_writing_agent(["claude", "--print"]) is False
    assert is_file_writing_agent(["claude", "-p"]) is False
    assert is_file_writing_agent(["other-ai"]) is False
    assert is_file_writing_agent([]) is False


def test_force_claude_print_appends_print():
    assert force_claude_print("claude --model opus") == "claude --model opus --print"


def test_force_claude_print_keeps_existing_print_flags():
    assert force_claude_print("claude --print") == "claude --print"
    assert force_claude_print("claude -p") == "claude -p"


def test_force_claude_print_leaves_non_claude_unchanged():
    assert force_claude_print("codex exec -") == "codex exec -"


def test_prepare_read_only_codex_hardens_codex_exec(tmp_path):
    command = (
        "codex exec --full-auto --model gpt-5.5 "
        "-c 'reasoning_effort=\"medium\"' "
        "--dangerously-bypass-approvals-and-sandbox "
        "--cd /project -"
    )

    resolved = prepare_read_only_codex(command, tmp_path)

    assert "--full-auto" not in resolved
    assert "--dangerously-bypass-approvals-and-sandbox" not in resolved
    assert "--cd /project" not in resolved
    assert "--sandbox read-only" in resolved
    assert tmp_path.as_posix() in resolved
    assert "--skip-git-repo-check" in resolved
    assert "--ephemeral" in resolved
    assert resolved.endswith(" -")


def test_prepare_read_only_codex_leaves_non_codex_command_unchanged(tmp_path):
    assert prepare_read_only_codex("claude --print", tmp_path) == "claude --print"


def test_is_codex_exec_command():
    assert is_codex_exec_command(["codex", "exec"]) is True
    assert is_codex_exec_command(["/usr/bin/codex", "exec", "-"]) is True
    assert is_codex_exec_command(["codex", "app-server"]) is False
    assert is_codex_exec_command(["claude", "exec"]) is False
    assert is_codex_exec_command(["codex"]) is False


# ═══════════════════════════════════════════════════════════
# invoke_ai: subprocess path
# ═══════════════════════════════════════════════════════════


def _completed(command, returncode=0, stdout="ai output", stderr=""):
    return subprocess.CompletedProcess(args=command, returncode=returncode, stdout=stdout, stderr=stderr)


def test_invoke_ai_returns_stdout(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(command)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    assert invoke_ai("mock-ai --print", "prompt") == "ai output"
    assert calls == [["mock-ai", "--print"]]


def test_invoke_ai_applies_claude_permission_bypass(monkeypatch):
    monkeypatch.delenv("CODD_CLAUDE_SAFE_PERMISSIONS", raising=False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(command)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    invoke_ai("claude --print", "prompt")

    command = calls[0]
    assert "--permission-mode" in command
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" in command


def test_invoke_ai_rejects_empty_command():
    with pytest.raises(ValueError, match="must not be empty"):
        invoke_ai("   ", "prompt")


def test_invoke_ai_nonzero_exit_raises_ai_command_failed(monkeypatch):
    monkeypatch.setattr(
        ai_invoke.subprocess, "run",
        lambda command, **kwargs: _completed(command, returncode=1, stdout="", stderr="boom"),
    )

    with pytest.raises(ValueError, match="AI command failed: boom"):
        invoke_ai("mock-ai", "prompt")


def test_invoke_ai_empty_output_raises(monkeypatch):
    monkeypatch.setattr(
        ai_invoke.subprocess, "run",
        lambda command, **kwargs: _completed(command, stdout="   "),
    )

    with pytest.raises(ValueError, match="empty output"):
        invoke_ai("mock-ai", "prompt")


def test_invoke_ai_missing_binary_raises_not_found(monkeypatch):
    def fake_run(command, **kwargs):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="AI command not found: missing-ai"):
        invoke_ai("missing-ai", "prompt")


# ═══════════════════════════════════════════════════════════
# invoke_ai: bounded retry on transient failures
# ═══════════════════════════════════════════════════════════


def test_invoke_ai_retries_after_nonzero_exit_then_succeeds(monkeypatch):
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        if len(attempts) == 1:
            return _completed(command, returncode=1, stderr="transient")
        return _completed(command, stdout="recovered")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    assert invoke_ai("mock-ai", "prompt", retries=2) == "recovered"
    assert len(attempts) == 2


def test_invoke_ai_retries_after_empty_output_then_succeeds(monkeypatch):
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        if len(attempts) <= 2:
            return _completed(command, stdout="")
        return _completed(command, stdout="recovered")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    assert invoke_ai("mock-ai", "prompt", retries=2) == "recovered"
    assert len(attempts) == 3


def test_invoke_ai_exhausted_retries_raise_last_error(monkeypatch):
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        return _completed(command, returncode=1, stderr="always broken")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="AI command failed: always broken"):
        invoke_ai("mock-ai", "prompt", retries=2)
    assert len(attempts) == 3  # 1 attempt + 2 retries


def test_invoke_ai_does_not_retry_missing_binary(monkeypatch):
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="AI command not found"):
        invoke_ai("missing-ai", "prompt", retries=5)
    assert len(attempts) == 1  # permanent failure: no retry


def test_invoke_ai_zero_retries_is_single_attempt(monkeypatch):
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        return _completed(command, returncode=1, stderr="boom")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="AI command failed"):
        invoke_ai("mock-ai", "prompt")
    assert len(attempts) == 1


def test_invoke_ai_retry_feedback_rewrites_prompt(monkeypatch):
    prompts = []

    def fake_run(command, *, input, **kwargs):
        prompts.append(input)
        if len(prompts) == 1:
            return _completed(command, stdout="")
        return _completed(command, stdout="recovered")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    def feedback(prompt, error, attempt):
        return f"{prompt}\n\nRETRY {attempt}: {error}"

    assert invoke_ai("mock-ai", "base prompt", retries=1, retry_feedback=feedback) == "recovered"
    assert prompts[0] == "base prompt"
    assert "RETRY 0: AI command returned empty output" in prompts[1]


# ═══════════════════════════════════════════════════════════
# invoke_ai: file-writing-agent routing
# ═══════════════════════════════════════════════════════════


def test_invoke_ai_routes_file_writing_agent_when_project_root_given(monkeypatch, tmp_path):
    routed = {}

    def fake_file_writing(command, prompt, project_root):
        routed["command"] = command
        routed["prompt"] = prompt
        routed["project_root"] = project_root
        return "=== FILE: src/x.py ===\ncontent\n"

    monkeypatch.setattr(ai_invoke, "invoke_file_writing_agent", fake_file_writing)

    result = invoke_ai("codex exec -", "prompt", project_root=tmp_path)

    assert result.startswith("=== FILE: src/x.py ===")
    assert routed["command"][0] == "codex"
    assert routed["project_root"] == tmp_path


def test_invoke_ai_does_not_route_file_writing_without_project_root(monkeypatch):
    def fail_file_writing(command, prompt, project_root):  # pragma: no cover - must not run
        raise AssertionError("file-writing routing must not trigger without project_root")

    monkeypatch.setattr(ai_invoke, "invoke_file_writing_agent", fail_file_writing)
    monkeypatch.setattr(
        ai_invoke.subprocess, "run", lambda command, **kwargs: _completed(command)
    )

    assert invoke_ai("codex exec -", "prompt") == "ai output"


# ═══════════════════════════════════════════════════════════
# invoke_file_writing_agent: stdout-contract fallback (D6 cross-CLI)
# ═══════════════════════════════════════════════════════════
#
# A CLI classified as "file-writing" (e.g. ``codex exec``) may, under a
# read-only / text-out invocation, honour the prompt's stdout contract and emit
# ``=== FILE: ===`` blocks on stdout while writing NOTHING to disk. Before the
# D6 fix, invoke_file_writing_agent read results only from ``git diff`` and so
# failed with "AI command did not produce any file changes", even though the
# agent produced exactly the output CoDD's file-block parser consumes. The
# fallback honours whichever channel the agent used, keeping the path
# CLI-agnostic. (Generality gate: the git/on-disk path is unchanged, so the
# file-writing Claude/codex behaviour does not regress.)


def _file_writing_dispatch(*, agent_stdout, agent_returncode=0, changed="", untracked=""):
    """Build a subprocess.run fake for invoke_file_writing_agent.

    Routes git subcommands to canned outputs and the agent command to
    *agent_stdout*. Records every git subcommand list in ``calls``.
    """
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        if command and command[0] == "git":
            calls.append(command)
            sub = command[1] if len(command) > 1 else ""
            if sub == "diff":
                return _completed(command, stdout=changed)
            if sub == "ls-files":
                return _completed(command, stdout=untracked)
            return _completed(command, stdout="")  # add -A, reset, checkout
        # the AI agent invocation
        return _completed(command, returncode=agent_returncode, stdout=agent_stdout)

    return fake_run, calls


def test_file_writing_agent_falls_back_to_stdout_contract(monkeypatch, tmp_path):
    stdout = "=== FILE: src/greeter/cli.py ===\n```python\nprint('hi')\n```\n"
    fake_run, calls = _file_writing_dispatch(agent_stdout=stdout)
    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    result = invoke_file_writing_agent(["codex", "exec", "-"], "prompt", tmp_path)

    # The stdout contract is returned verbatim (CoDD's parser consumes it).
    assert result == stdout
    # Working tree is left clean: the staged baseline is unstaged via git reset.
    assert ["git", "reset", "--quiet"] in calls


def test_file_writing_agent_prefers_on_disk_writes_over_stdout(monkeypatch, tmp_path):
    # When the agent ALSO wrote a file to disk, the on-disk content wins
    # (unchanged behaviour — Claude/codex that write files keep working).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("ON_DISK = 1\n", encoding="utf-8")
    fake_run, _calls = _file_writing_dispatch(
        agent_stdout="=== FILE: ignored/stdout.py ===\nstdout body\n",
        untracked="src/real.py",
    )
    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    result = invoke_file_writing_agent(["codex", "exec", "-"], "prompt", tmp_path)

    assert "=== FILE: src/real.py ===" in result
    assert "ON_DISK = 1" in result
    assert "ignored/stdout.py" not in result


def test_file_writing_agent_no_files_and_no_contract_still_raises(monkeypatch, tmp_path):
    fake_run, _calls = _file_writing_dispatch(agent_stdout="I will think about it...\n")
    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="did not produce any file changes"):
        invoke_file_writing_agent(["codex", "exec", "-"], "prompt", tmp_path)


def test_stdout_carries_file_contract_detector():
    assert ai_invoke._stdout_carries_file_contract("=== FILE: a.py ===\nx\n") is True
    assert ai_invoke._stdout_carries_file_contract("prose only, no blocks") is False
    assert ai_invoke._stdout_carries_file_contract("") is False


# ═══════════════════════════════════════════════════════════
# invoke_ai: codex read-only hardening + claude --print forcing
# ═══════════════════════════════════════════════════════════


def test_invoke_ai_hardens_codex_exec_and_skips_file_writing_routing(monkeypatch, tmp_path):
    calls = []

    def fail_file_writing(command, prompt, project_root):  # pragma: no cover - must not run
        raise AssertionError("hardened call must stay plain text-in/text-out")

    monkeypatch.setattr(ai_invoke, "invoke_file_writing_agent", fail_file_writing)

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(command)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    result = invoke_ai(
        "codex exec --full-auto -",
        "prompt",
        project_root=tmp_path,
        harden_read_only=True,
        safe_root=tmp_path,
    )

    assert result == "ai output"
    command = calls[0]
    assert "--full-auto" not in command
    sandbox_index = command.index("--sandbox")
    assert command[sandbox_index + 1] == "read-only"
    assert tmp_path.as_posix() in command
    assert "--skip-git-repo-check" in command
    assert command[-1] == "-"


def test_invoke_ai_harden_creates_temporary_workspace_when_no_safe_root(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(command)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    invoke_ai("codex exec -", "prompt", harden_read_only=True)

    command = calls[0]
    workspace = Path(command[command.index("--cd") + 1])
    assert workspace.name.startswith("codd-ai-")
    assert not workspace.exists()  # cleaned up after the call


def test_invoke_ai_forces_print_on_claude(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(command)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    invoke_ai("claude --model opus", "prompt", force_print_on_claude=True)

    assert "--print" in calls[0]


def test_invoke_ai_force_print_leaves_non_claude_unchanged(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(command)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    invoke_ai("mock-ai --json", "prompt", force_print_on_claude=True)

    assert calls[0] == ["mock-ai", "--json"]


# ═══════════════════════════════════════════════════════════
# invoke_ai: deployment adapter routing (Codex App Server transport)
# ═══════════════════════════════════════════════════════════


def test_invoke_ai_with_config_routes_through_adapter_factory(monkeypatch, tmp_path):
    import codd.deployment.providers.ai_command_factory as factory

    captured = {}

    class RecordingAdapter:
        def invoke(self, prompt):
            captured["prompt"] = prompt
            return "adapter-output"

    def fake_get_ai_command(config, project_root=None, command_override=None):
        captured["config"] = config
        captured["project_root"] = project_root
        captured["command_override"] = command_override
        return RecordingAdapter()

    monkeypatch.setattr(factory, "get_ai_command", fake_get_ai_command)

    config = {"codex_app_server": {"enabled": True}}
    result = invoke_ai(
        "claude --model opus",
        "prompt",
        project_root=tmp_path,
        force_print_on_claude=True,
        config=config,
    )

    assert result == "adapter-output"
    assert captured["config"] == config
    assert captured["project_root"] == tmp_path
    assert captured["command_override"] == "claude --model opus --print"
    assert captured["prompt"] == "prompt"


def test_invoke_ai_with_disabled_app_server_uses_subprocess_adapter(monkeypatch):
    from codd.deployment.providers.ai_command import SubprocessAiCommand

    calls = []

    def fake_invoke(self, prompt, model=None, timeout=None):
        calls.append((self.command, prompt))
        return "subprocess-adapter-output"

    monkeypatch.setattr(SubprocessAiCommand, "invoke", fake_invoke)

    config = {"codex_app_server": {"enabled": False}}
    result = invoke_ai("mock-fix-ai", "prompt", config=config)

    assert result == "subprocess-adapter-output"
    assert calls == [("mock-fix-ai", "prompt")]


# ═══════════════════════════════════════════════════════════
# Backward-compat aliases + monkeypatch surfaces
# ═══════════════════════════════════════════════════════════


def test_generator_reexports_unified_functions():
    import codd.generator as generator_module

    assert generator_module._resolve_ai_command is ai_invoke.resolve_ai_command
    assert generator_module._invoke_ai_command is ai_invoke.invoke_ai
    assert generator_module._is_file_writing_agent is ai_invoke.is_file_writing_agent
    assert generator_module._invoke_file_writing_agent is ai_invoke.invoke_file_writing_agent
    assert generator_module.DEFAULT_AI_COMMAND == ai_invoke.DEFAULT_AI_COMMAND


def test_phenomenon_fixer_reexports_hardening_helpers():
    from codd.fix import phenomenon_fixer

    assert phenomenon_fixer._prepare_plain_text_ai_command is ai_invoke.prepare_read_only_codex
    assert phenomenon_fixer._is_codex_exec_command is ai_invoke.is_codex_exec_command


def test_monkeypatching_generator_invoke_ai_command_still_intercepts_generation(monkeypatch):
    """The classic test pattern: patch generator._invoke_ai_command, run generation."""
    import codd.generator as generator_module

    seen = {}

    def fake_invoke(ai_command, prompt, **kwargs):
        seen["ai_command"] = ai_command
        seen["prompt"] = prompt
        return (
            "# System Design\n\n## 1. Overview\n\nContent.\n\n"
            "## 2. Architecture\n\nArch.\n\n## 3. Open Questions\n\nNone.\n"
        )

    monkeypatch.setattr(generator_module, "_invoke_ai_command", fake_invoke)

    artifact = generator_module.WaveArtifact(
        wave=2,
        node_id="design:system-design",
        output="docs/design/system_design.md",
        title="System Design",
        depends_on=[],
        conventions=[],
    )

    body = generator_module._generate_document_body(artifact, [], [], "real-ai-never-called")

    assert "## 1. Overview" in body
    assert seen["ai_command"] == "real-ai-never-called"
    assert "System Design" in seen["prompt"]


def test_extract_ai_gains_bounded_retries(monkeypatch):
    """RF4 improvement: extract_ai retries transient failures (was single-shot)."""
    from codd import extract_ai

    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        if len(attempts) == 1:
            return _completed(command, returncode=1, stderr="transient blip")
        return _completed(command, stdout="--- FILE: L1_user_value.md ---\ncontent")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    output = extract_ai._invoke_ai_command("mock-ai", "prompt")

    assert "L1_user_value" in output
    assert len(attempts) == 2


# ═══════════════════════════════════════════════════════════
# invoke_ai: recoverable-error auto-recovery (transient transport + output ceiling)
# ═══════════════════════════════════════════════════════════
#
# Two recoverable CLI error classes observed in live greenfield runs forced a
# human to manually --resume / re-run with a raised output budget:
#   1. transient transport ("The socket connection was closed unexpectedly",
#      connection reset, timeout, 429/5xx) — re-running the identical call clears
#      it; and
#   2. the per-call output-token ceiling ("response exceeded the 32000 output
#      token maximum") — re-running with a raised CLAUDE_CODE_MAX_OUTPUT_TOKENS
#      clears it. invoke_ai now auto-recovers both at the shared chokepoint,
#      regardless of the caller's `retries`, so implement/generate/greenfield all
#      benefit. Permanent errors (auth/validation/missing binary) are never
#      retried. (Generality gate: classifier patterns are CLI-agnostic — they
#      match Claude- and Codex-style stderr alike, not one vendor.)


def test_classifier_transient_error_matches_recoverable_patterns():
    transient = [
        "API Error: The socket connection was closed unexpectedly",
        "Error: connection reset by peer",
        "read ECONNRESET",
        "request timed out after 600s",
        "API Error: 503 Service Unavailable",
        "429 Too Many Requests",
        "upstream gateway error",
        "the server is overloaded, please try again",
    ]
    for message in transient:
        assert ai_invoke._is_transient_error(message) is True, message
    permanent = [
        "401 Unauthorized: invalid API key",
        "authentication_error: missing credentials",
        "billing hard limit reached",
        "invalid_request_error: bad prompt schema",
        "AI command not found: claude",
        "",
    ]
    for message in permanent:
        assert ai_invoke._is_transient_error(message) is False, message


def test_classifier_output_ceiling_matches_ceiling_messages():
    ceilings = [
        "Claude's response exceeded the 32000 output token maximum",
        "max_output_tokens exceeded for this request",
        "set CLAUDE_CODE_MAX_OUTPUT_TOKENS to raise the limit",
        "the output token limit was reached",
    ]
    for message in ceilings:
        assert ai_invoke._is_output_ceiling_error(message) is True, message
    not_ceiling = [
        "input is too long: 200000 prompt tokens",
        "401 Unauthorized",
        "connection reset",
        "",
    ]
    for message in not_ceiling:
        assert ai_invoke._is_output_ceiling_error(message) is False, message


def test_invoke_ai_auto_retries_transient_socket_error_then_succeeds(monkeypatch):
    # Live symptom: "The socket connection was closed unexpectedly". The whole
    # task used to fail and a human had to --resume. It must now auto-retry.
    monkeypatch.setattr(ai_invoke.time, "sleep", lambda _s: None)  # no real backoff
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        if len(attempts) == 1:
            return _completed(
                command,
                returncode=1,
                stderr="API Error: The socket connection was closed unexpectedly",
            )
        return _completed(command, stdout="recovered after socket reset")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    # retries=0 (the default at every call site): recovery is independent of it.
    assert invoke_ai("mock-ai", "prompt") == "recovered after socket reset"
    assert len(attempts) == 2


def test_invoke_ai_transient_auto_retry_is_bounded(monkeypatch):
    monkeypatch.setattr(ai_invoke.time, "sleep", lambda _s: None)
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        return _completed(command, returncode=1, stderr="connection reset by peer")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="connection reset by peer"):
        invoke_ai("mock-ai", "prompt")
    # 1 initial attempt + TRANSIENT_AUTO_RETRIES bounded retries.
    assert len(attempts) == 1 + ai_invoke.TRANSIENT_AUTO_RETRIES


def test_invoke_ai_recovers_output_ceiling_by_raising_budget(monkeypatch):
    # Live symptom (F-output-32k): a single implement call aborts with the
    # 32000 output-token ceiling. The recovery re-issues the SAME call once with
    # a raised CLAUDE_CODE_MAX_OUTPUT_TOKENS in the child env — the documented
    # lever the human applied manually.
    monkeypatch.delenv(ai_invoke.OUTPUT_TOKENS_ENV, raising=False)
    envs_seen: list[str | None] = []

    def fake_run(command, **kwargs):
        env = kwargs.get("env")
        envs_seen.append(None if env is None else env.get(ai_invoke.OUTPUT_TOKENS_ENV))
        if len(envs_seen) == 1:
            return _completed(
                command,
                returncode=1,
                stderr="Claude's response exceeded the 32000 output token maximum",
            )
        return _completed(command, stdout="full untruncated output")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    assert invoke_ai("mock-ai", "prompt") == "full untruncated output"
    assert len(envs_seen) == 2
    # First call: default env (no override). Retry: raised budget passed through.
    assert envs_seen[0] is None
    assert envs_seen[1] == str(ai_invoke.RAISED_OUTPUT_TOKENS)


def test_invoke_ai_output_ceiling_not_retried_when_budget_already_high(monkeypatch):
    # If the budget is ALREADY at/above the raised value, a genuine ceiling must
    # surface honestly instead of looping forever.
    monkeypatch.setenv(ai_invoke.OUTPUT_TOKENS_ENV, str(ai_invoke.RAISED_OUTPUT_TOKENS))
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        return _completed(
            command,
            returncode=1,
            stderr="response exceeded the 64000 output token maximum",
        )

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="output token maximum"):
        invoke_ai("mock-ai", "prompt")
    assert len(attempts) == 1  # no pointless re-issue at an already-high budget


def test_invoke_ai_does_not_auto_retry_permanent_auth_error(monkeypatch):
    # Mirrors the "missing binary is permanent" rule: auth/validation errors are
    # NOT transient and must fail immediately without burning auto-retries. With
    # retries=0 (the default at every call site) the new recovery layer is the
    # only thing that could re-issue the call — and it must not, for a permanent
    # error. (The separate `retries`/feedback loop is exercised elsewhere.)
    monkeypatch.setattr(ai_invoke.time, "sleep", lambda _s: None)
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        return _completed(
            command,
            returncode=1,
            stderr="authentication_error: invalid x-api-key (401 Unauthorized)",
        )

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="authentication_error"):
        invoke_ai("mock-ai", "prompt")
    assert len(attempts) == 1  # permanent: no transient/ceiling recovery


def test_invoke_ai_output_ceiling_recovery_threads_to_file_writing_agent(monkeypatch, tmp_path):
    # The output-ceiling env-raise must reach file-writing CLIs (codex) too: the
    # raised budget is threaded into invoke_file_writing_agent's child env.
    monkeypatch.delenv(ai_invoke.OUTPUT_TOKENS_ENV, raising=False)
    agent_envs: list[str | None] = []

    def fake_run(command, **kwargs):
        if command and command[0] == "git":
            sub = command[1] if len(command) > 1 else ""
            if sub == "diff":
                return _completed(command, stdout="")
            if sub == "ls-files":
                return _completed(command, stdout="")
            return _completed(command, stdout="")
        # the AI agent invocation
        env = kwargs.get("env")
        agent_envs.append(None if env is None else env.get(ai_invoke.OUTPUT_TOKENS_ENV))
        if len(agent_envs) == 1:
            return _completed(
                command,
                returncode=1,
                stderr="response exceeded the 32000 output token maximum",
            )
        return _completed(command, stdout="=== FILE: src/x.py ===\nbody\n")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    result = invoke_ai("codex exec -", "prompt", project_root=tmp_path)

    assert "=== FILE: src/x.py ===" in result
    assert agent_envs[0] is None
    assert agent_envs[-1] == str(ai_invoke.RAISED_OUTPUT_TOKENS)


# ═══════════════════════════════════════════════════════════
# invoke_ai: wall-clock timeout on a silently-hung AI call (anti-hang)
# ═══════════════════════════════════════════════════════════
#
# Live symptom (F-ai-call-hang, axis D7): a model CLI subprocess stalled on a
# single call for 47min+ — process Sl, ~0.9% CPU, blocked on I/O, NO error and
# NO output. The recoverable-error retry never fired (nothing was returned to
# classify), so the whole pipeline froze forever. Root cause: AI subprocess
# calls had no wall-clock timeout. The fix gives every AI subprocess call a
# finite timeout; a stall raises subprocess.TimeoutExpired (which kills the
# spawned child), and the timeout is treated as a TRANSIENT transport failure so
# it flows into the SAME bounded auto-retry/backoff as a dropped socket. A
# one-off stall self-recovers; a persistently-hung call fails loudly after the
# bounded attempts instead of hanging.


def _timeout_expired(command, timeout):
    """A subprocess.TimeoutExpired exactly as subprocess.run raises it.

    subprocess.run kills and reaps the child before raising this, so simulating
    the exception is the faithful "the call hung past the wall-clock budget"
    fake — no real child, no real wait.
    """
    return subprocess.TimeoutExpired(cmd=command, timeout=timeout)


def test_default_call_timeout_matches_shared_ssot():
    # The direct-subprocess timeout default is the same SSoT the deployment
    # adapter uses, so every AI call site agrees by default.
    from codd.defaults import AI_TIMEOUT_SECONDS

    assert ai_invoke.DEFAULT_AI_CALL_TIMEOUT_SECONDS == AI_TIMEOUT_SECONDS == 3600.0


def test_resolve_ai_call_timeout_precedence(monkeypatch):
    monkeypatch.delenv(ai_invoke.AI_CALL_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(ai_invoke.AI_TIMEOUT_ENV, raising=False)

    # default when nothing set
    assert ai_invoke.resolve_ai_call_timeout() == ai_invoke.DEFAULT_AI_CALL_TIMEOUT_SECONDS
    # config keys: dedicated ai.call_timeout_seconds wins over llm.timeout_seconds
    assert ai_invoke.resolve_ai_call_timeout({"llm": {"timeout_seconds": 600}}) == 600.0
    assert (
        ai_invoke.resolve_ai_call_timeout(
            {"ai": {"call_timeout_seconds": 1200}, "llm": {"timeout_seconds": 600}}
        )
        == 1200.0
    )
    # explicit override beats everything
    assert ai_invoke.resolve_ai_call_timeout({"ai": {"call_timeout_seconds": 1200}}, override=42) == 42.0


def test_resolve_ai_call_timeout_env_overrides(monkeypatch):
    # dedicated env beats shared env beats config
    monkeypatch.setenv(ai_invoke.AI_TIMEOUT_ENV, "777")
    assert ai_invoke.resolve_ai_call_timeout({"ai": {"call_timeout_seconds": 1200}}) == 777.0
    monkeypatch.setenv(ai_invoke.AI_CALL_TIMEOUT_ENV, "900")
    assert ai_invoke.resolve_ai_call_timeout({"ai": {"call_timeout_seconds": 1200}}) == 900.0


def test_resolve_ai_call_timeout_ignores_garbage_and_nonpositive(monkeypatch):
    # blank/unparseable/zero/negative are ignored (fall through) — never disable
    # the timeout. Falls back to the default here.
    monkeypatch.setenv(ai_invoke.AI_CALL_TIMEOUT_ENV, "not-a-number")
    monkeypatch.delenv(ai_invoke.AI_TIMEOUT_ENV, raising=False)
    assert ai_invoke.resolve_ai_call_timeout() == ai_invoke.DEFAULT_AI_CALL_TIMEOUT_SECONDS
    assert ai_invoke.resolve_ai_call_timeout({"ai": {"call_timeout_seconds": 0}}) == \
        ai_invoke.DEFAULT_AI_CALL_TIMEOUT_SECONDS
    assert ai_invoke.resolve_ai_call_timeout({"ai": {"call_timeout_seconds": -5}}) == \
        ai_invoke.DEFAULT_AI_CALL_TIMEOUT_SECONDS


def test_invoke_ai_passes_wall_clock_timeout_to_subprocess(monkeypatch):
    # Every AI subprocess call must carry a finite timeout= so a silent hang
    # cannot block forever.
    monkeypatch.delenv(ai_invoke.AI_CALL_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(ai_invoke.AI_TIMEOUT_ENV, raising=False)
    seen = {}

    def fake_run(command, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return _completed(command)

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    invoke_ai("mock-ai", "prompt")
    assert seen["timeout"] == ai_invoke.DEFAULT_AI_CALL_TIMEOUT_SECONDS
    assert seen["timeout"] is not None and seen["timeout"] > 0


def test_invoke_ai_timeout_then_succeeds(monkeypatch):
    # (a) A call that exceeds the timeout is killed (subprocess.run does this on
    # TimeoutExpired) and the one-off stall self-recovers on auto-retry.
    monkeypatch.setattr(ai_invoke.time, "sleep", lambda _s: None)  # no real backoff
    monkeypatch.delenv(ai_invoke.AI_CALL_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(ai_invoke.AI_TIMEOUT_ENV, raising=False)
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        if len(attempts) == 1:
            raise _timeout_expired(command, kwargs.get("timeout"))
        return _completed(command, stdout="recovered after a hang")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    # retries=0 (the default at every call site): recovery is independent of it.
    assert invoke_ai("mock-ai", "prompt") == "recovered after a hang"
    assert len(attempts) == 2


def test_invoke_ai_persistent_timeout_fails_after_bounded_attempts(monkeypatch):
    # (b) If the call keeps timing out, it must FAIL after the bounded attempts
    # with a clear "AI call timed out after Ns" message — NOT hang forever.
    monkeypatch.setattr(ai_invoke.time, "sleep", lambda _s: None)
    monkeypatch.setenv(ai_invoke.AI_CALL_TIMEOUT_ENV, "5")  # tiny, deterministic
    attempts = []

    def fake_run(command, **kwargs):
        attempts.append(command)
        raise _timeout_expired(command, kwargs.get("timeout"))

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match=r"AI call timed out after 5s") as excinfo:
        invoke_ai("mock-ai", "prompt")
    # bounded: 1 initial attempt + TRANSIENT_AUTO_RETRIES, then it gives up.
    assert len(attempts) == 1 + ai_invoke.TRANSIENT_AUTO_RETRIES
    # the message is routed through the transient classifier (clear, finite).
    assert "AI command failed" in str(excinfo.value)


def test_invoke_ai_timeout_is_classified_transient():
    # The timeout failure text must match the transient classifier so it routes
    # into the SAME recovery path as a dropped socket (no parallel path).
    msg = ai_invoke._AI_CALL_TIMEOUT_MESSAGE.format(seconds="5")
    assert ai_invoke._is_transient_error(msg)


def test_invoke_ai_each_retry_gets_fresh_timeout(monkeypatch):
    # Per-call: the timeout= is supplied on EVERY subprocess attempt, not once.
    monkeypatch.setattr(ai_invoke.time, "sleep", lambda _s: None)
    monkeypatch.setenv(ai_invoke.AI_CALL_TIMEOUT_ENV, "5")
    timeouts = []

    def fake_run(command, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        if len(timeouts) < 3:
            raise _timeout_expired(command, kwargs.get("timeout"))
        return _completed(command, stdout="ok")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    assert invoke_ai("mock-ai", "prompt") == "ok"
    assert timeouts == [5.0, 5.0, 5.0]  # fresh, finite budget each attempt


def test_invoke_file_writing_agent_timeout_routes_to_transient_retry(monkeypatch, tmp_path):
    # The file-writing-agent path (codex / interactive claude) must ALSO carry a
    # wall-clock timeout and route a stall into the same transient auto-retry.
    monkeypatch.setattr(ai_invoke.time, "sleep", lambda _s: None)
    monkeypatch.setenv(ai_invoke.AI_CALL_TIMEOUT_ENV, "5")
    agent_attempts = []

    def fake_run(command, **kwargs):
        if command and command[0] == "git":
            return _completed(command, stdout="")
        # the AI agent invocation
        agent_attempts.append(kwargs.get("timeout"))
        if len(agent_attempts) == 1:
            raise _timeout_expired(command, kwargs.get("timeout"))
        return _completed(command, stdout="=== FILE: src/x.py ===\nbody\n")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    result = invoke_ai("codex exec -", "prompt", project_root=tmp_path)
    assert "=== FILE: src/x.py ===" in result
    # the agent's AI call carried a finite timeout on the first (hung) attempt.
    assert agent_attempts[0] == 5.0


def test_invoke_file_writing_agent_persistent_timeout_fails_clearly(monkeypatch, tmp_path):
    # A persistently-hung file-writing agent fails loudly with the timeout
    # message after the bounded attempts — never an infinite hang.
    monkeypatch.setattr(ai_invoke.time, "sleep", lambda _s: None)
    monkeypatch.setenv(ai_invoke.AI_CALL_TIMEOUT_ENV, "5")

    def fake_run(command, **kwargs):
        if command and command[0] == "git":
            return _completed(command, stdout="")
        raise _timeout_expired(command, kwargs.get("timeout"))

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match=r"AI call timed out after 5s"):
        invoke_ai("codex exec -", "prompt", project_root=tmp_path)


def test_invoke_file_writing_agent_default_timeout_is_finite(monkeypatch, tmp_path):
    # When no override is set, the file-writing path still self-resolves the
    # shared finite default (regression: it must never run unbounded).
    monkeypatch.delenv(ai_invoke.AI_CALL_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(ai_invoke.AI_TIMEOUT_ENV, raising=False)
    seen = {}

    def fake_run(command, **kwargs):
        if command and command[0] == "git":
            return _completed(command, stdout="")
        seen["timeout"] = kwargs.get("timeout")
        return _completed(command, stdout="=== FILE: src/x.py ===\nbody\n")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)

    invoke_ai("codex exec -", "prompt", project_root=tmp_path)
    assert seen["timeout"] == ai_invoke.DEFAULT_AI_CALL_TIMEOUT_SECONDS
