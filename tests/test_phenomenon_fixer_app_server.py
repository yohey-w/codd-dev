from __future__ import annotations

from pathlib import Path

import pytest

from codd.deployment.providers.ai_command import CodexAppServerAiCommand, SubprocessAiCommand
from codd.deployment.providers.codex_app_server import CodexAppServerInitError
import codd.deployment.providers.ai_command_factory as factory
from codd.fix import phenomenon_fixer


class RecordingClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.start_calls = 0
        self.send_calls: list[tuple[str, str, float | None]] = []
        self.archived: list[str] = []
        self.closed = 0

    async def start_thread(self, model, effort, cwd, base_instructions) -> str:
        self.start_calls += 1
        return "thread-1"

    async def send_turn(self, thread_id, input, timeout, *, model=None, effort=None, cwd=None) -> str:
        self.send_calls.append((thread_id, input, timeout))
        return self.response

    async def archive_thread(self, thread_id) -> None:
        self.archived.append(thread_id)

    async def close(self) -> None:
        self.closed += 1


def test_phenomenon_fixer_disabled_app_server_invokes_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    config = {"ai_commands": {"fix": "mock-fix-ai"}}

    def fake_invoke(self, prompt: str, model: str | None = None, timeout: float | None = None) -> str:
        calls.append((self.command, self.project_root, self.config, prompt))
        return "subprocess-output"

    monkeypatch.setattr(phenomenon_fixer, "load_project_config", lambda _project_root: config)
    monkeypatch.setattr(SubprocessAiCommand, "invoke", fake_invoke)

    invoker = phenomenon_fixer._build_default_ai_invoke(tmp_path, None)

    assert invoker("phenomenon prompt") == "subprocess-output"
    assert calls == [("mock-fix-ai", None, config, "phenomenon prompt")]


def test_phenomenon_fixer_enabled_app_server_invokes_codex_app_server_ai_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RecordingClient("app-server-output")
    adapters: list[CodexAppServerAiCommand] = []
    project_roots: list[Path | str | None] = []
    config = {
        "ai_commands": {"fix": "mock-fix-ai"},
        "codex_app_server": {"enabled": True, "transport": "stdio", "thread_strategy": "per_session"},
    }

    def fake_from_config(section, project_root):
        project_roots.append(project_root)
        adapter = CodexAppServerAiCommand(
            client,
            project_root=project_root,
            thread_strategy=section.get("thread_strategy", "per_session"),
        )
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(phenomenon_fixer, "load_project_config", lambda _project_root: config)
    monkeypatch.setattr(factory.shutil, "which", lambda _binary: "/usr/bin/codex")
    monkeypatch.setattr(CodexAppServerAiCommand, "from_config", staticmethod(fake_from_config))

    invoker = phenomenon_fixer._build_default_ai_invoke(tmp_path, None)

    assert invoker("phenomenon prompt") == "app-server-output"
    assert project_roots == [None]
    assert client.start_calls == 1
    assert client.send_calls[0][0] == "thread-1"
    assert client.send_calls[0][1] == "phenomenon prompt"

    adapters[0].close()
    assert client.archived == ["thread-1"]
    assert client.closed == 1


def test_phenomenon_fixer_app_server_init_failure_falls_back_to_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = []
    config = {
        "ai_commands": {"fix": "mock-fix-ai"},
        "codex_app_server": {"enabled": True, "transport": "stdio", "fallback": "subprocess"},
    }

    def fake_from_config(section, project_root):
        raise CodexAppServerInitError("app server unavailable")

    def fake_invoke(self, prompt: str, model: str | None = None, timeout: float | None = None) -> str:
        calls.append((self.command, self.project_root, self.config, prompt))
        return "fallback-output"

    monkeypatch.setattr(phenomenon_fixer, "load_project_config", lambda _project_root: config)
    monkeypatch.setattr(factory.shutil, "which", lambda _binary: "/usr/bin/codex")
    monkeypatch.setattr(CodexAppServerAiCommand, "from_config", staticmethod(fake_from_config))
    monkeypatch.setattr(SubprocessAiCommand, "invoke", fake_invoke)

    with caplog.at_level("WARNING"):
        invoker = phenomenon_fixer._build_default_ai_invoke(tmp_path, None)

    assert invoker("phenomenon prompt") == "fallback-output"
    assert calls == [("mock-fix-ai", None, config, "phenomenon prompt")]
    assert "Codex App Server fallback" in caplog.text
    assert "app server unavailable" in caplog.text


def test_phenomenon_fixer_claude_print_flag_is_passed_as_factory_command_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    config = {"ai_commands": {"fix": "claude --model opus"}}

    class RecordingAdapter:
        def invoke(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "adapter-output"

    def fake_get_ai_command(config_arg, project_root=None, command_override=None):
        captured["config"] = config_arg
        captured["project_root"] = project_root
        captured["command_override"] = command_override
        return RecordingAdapter()

    monkeypatch.setattr(phenomenon_fixer, "load_project_config", lambda _project_root: config)
    monkeypatch.setattr(phenomenon_fixer, "get_ai_command", fake_get_ai_command)

    invoker = phenomenon_fixer._build_default_ai_invoke(tmp_path, None)

    assert invoker("phenomenon prompt") == "adapter-output"
    assert captured["config"] == config
    assert captured["project_root"] is None
    assert captured["command_override"] == "claude --model opus --print"
    assert captured["prompt"] == "phenomenon prompt"
