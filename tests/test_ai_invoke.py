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
