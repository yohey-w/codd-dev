from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codd.deployment.providers.ai_command import CodexAppServerAiCommand, SubprocessAiCommand
from codd.deployment.providers.codex_app_server import CodexAppServerInitError, CodexAppServerTimeout
import codd.deployment.providers.ai_command_factory as factory


class RecordingClient:
    def __init__(self) -> None:
        self.start_calls = 0
        self.send_calls: list[tuple[str, str, float | None]] = []
        self.archived: list[str] = []
        self.closed = 0

    async def start_thread(self, model, effort, cwd, base_instructions) -> str:
        self.start_calls += 1
        return "thread-1"

    async def send_turn(self, thread_id, input, timeout, *, model=None, effort=None, cwd=None) -> str:
        self.send_calls.append((thread_id, input, timeout))
        return f"agent:{thread_id}:{input}"

    async def archive_thread(self, thread_id) -> None:
        self.archived.append(thread_id)

    async def close(self) -> None:
        self.closed += 1


class TimeoutClient(RecordingClient):
    async def send_turn(self, thread_id, input, timeout, *, model=None, effort=None, cwd=None) -> str:
        raise CodexAppServerTimeout("timeout")


class FallbackCommand:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, float | None]] = []

    def invoke(self, prompt: str, model: str | None = None, timeout: float | None = None) -> str:
        self.calls.append((prompt, model, timeout))
        return "fallback-output"

    def close(self) -> None:
        return None


def test_enabled_false_returns_subprocess_ai_command(tmp_path: Path) -> None:
    command = factory.get_ai_command({"codex_app_server": {"enabled": False}}, tmp_path, command_override="mock-ai")

    assert isinstance(command, SubprocessAiCommand)
    assert command.command == "mock-ai"


def test_enabled_true_without_codex_binary_warns_and_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(factory.shutil, "which", lambda _binary: None)

    command = factory.get_ai_command({"codex_app_server": {"enabled": True}}, tmp_path)

    assert isinstance(command, SubprocessAiCommand)
    assert "Codex App Server fallback" in caplog.text
    assert "binary not found" in caplog.text


def test_factory_returns_codex_app_server_ai_command_and_invoke_reuses_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RecordingClient()

    def fake_from_config(section, project_root):
        return CodexAppServerAiCommand(
            client,
            project_root=project_root,
            thread_strategy=section.get("thread_strategy", "per_session"),
        )

    monkeypatch.setattr(factory.shutil, "which", lambda _binary: "/usr/bin/codex")
    monkeypatch.setattr(CodexAppServerAiCommand, "from_config", staticmethod(fake_from_config))

    command = factory.get_ai_command(
        {"codex_app_server": {"enabled": True, "transport": "stdio", "thread_strategy": "per_session"}},
        tmp_path,
    )

    assert isinstance(command, CodexAppServerAiCommand)
    assert command.invoke("first") == "agent:thread-1:first"
    assert command.invoke("second") == "agent:thread-1:second"
    assert client.start_calls == 1
    command.close()
    assert client.archived == ["thread-1"]
    assert client.closed == 1


def test_turn_timeout_falls_back_to_subprocess_command() -> None:
    fallback = FallbackCommand()
    command = CodexAppServerAiCommand(
        TimeoutClient(),
        fallback="subprocess",
        fallback_command=fallback,
        model="gpt-5.5",
    )

    assert command.invoke("prompt", model="override-model", timeout=1.5) == "fallback-output"
    assert fallback.calls == [("prompt", "override-model", 1.5)]


def test_fallback_error_raises_when_codex_binary_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factory.shutil, "which", lambda _binary: None)

    with pytest.raises(CodexAppServerInitError, match="binary not found"):
        factory.get_ai_command({"codex_app_server": {"enabled": True, "fallback": "error"}}, tmp_path)


def test_subprocess_ai_command_direct_new_still_works() -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    command = SubprocessAiCommand(command="mock-ai", runner=fake_run)

    assert command.invoke("prompt", model="m1", timeout=12.0) == "ok"
    assert calls[0][0] == ["mock-ai", "--model", "m1"]
    assert calls[0][1]["timeout"] == 12.0
