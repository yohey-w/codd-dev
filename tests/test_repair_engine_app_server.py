from __future__ import annotations

from pathlib import Path

import pytest

from codd.dag import DAG, Edge, Node
from codd.deployment.providers.ai_command import CodexAppServerAiCommand, SubprocessAiCommand
from codd.deployment.providers.codex_app_server import CodexAppServerInitError
import codd.deployment.providers.ai_command_factory as factory
from codd.repair.llm_repair_engine import LlmRepairEngine
from codd.repair.schema import VerificationFailureReport


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


def _analysis_response(cause: str) -> str:
    return (
        '{"probable_cause":"'
        + cause
        + '","affected_nodes":["impl:login"],"repair_strategy":"unified_diff","confidence":0.7}'
    )


def _failure() -> VerificationFailureReport:
    return VerificationFailureReport(
        check_name="node_completeness",
        failed_nodes=["design:login"],
        error_messages=["implementation node missing"],
        dag_snapshot={"nodes": [{"id": "design:login"}], "edges": []},
        timestamp="2026-05-18T00:00:00Z",
    )


def _dag() -> DAG:
    dag = DAG()
    dag.add_node(Node("design:login", "design_doc", "docs/design.md", {"capability": "sign_in"}))
    dag.add_node(Node("impl:login", "impl_file", "src/login.py", {}))
    dag.add_edge(Edge("design:login", "impl:login", "expects"))
    return dag


def test_repair_engine_disabled_app_server_invokes_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_invoke(self, prompt: str, model: str | None = None, timeout: float | None = None) -> str:
        calls.append((self.command, self.project_root, self.config, prompt))
        return _analysis_response("subprocess")

    monkeypatch.setattr(SubprocessAiCommand, "invoke", fake_invoke)
    config = {
        "ai_commands": {"repair_analyze": "mock-repair-ai"},
        "codex_app_server": {"enabled": False},
    }

    result = LlmRepairEngine(project_root=tmp_path, config=config).analyze(_failure(), _dag())

    assert result.probable_cause == "subprocess"
    assert calls[0][0] == "mock-repair-ai"
    assert calls[0][1] == tmp_path
    assert calls[0][2] == config


def test_repair_engine_enabled_app_server_invokes_codex_app_server_ai_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RecordingClient(_analysis_response("app-server"))

    def fake_from_config(section, project_root):
        return CodexAppServerAiCommand(
            client,
            project_root=project_root,
            thread_strategy=section.get("thread_strategy", "per_session"),
        )

    monkeypatch.setattr(factory.shutil, "which", lambda _binary: "/usr/bin/codex")
    monkeypatch.setattr(CodexAppServerAiCommand, "from_config", staticmethod(fake_from_config))
    config = {
        "ai_commands": {"repair_analyze": "mock-repair-ai"},
        "codex_app_server": {"enabled": True, "transport": "stdio", "thread_strategy": "per_session"},
    }

    result = LlmRepairEngine(project_root=tmp_path, config=config).analyze(_failure(), _dag())

    assert result.probable_cause == "app-server"
    assert client.start_calls == 1
    assert client.send_calls[0][0] == "thread-1"
    assert "node_completeness" in client.send_calls[0][1]


def test_repair_engine_app_server_init_failure_falls_back_to_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = []

    def fake_from_config(section, project_root):
        raise CodexAppServerInitError("app server unavailable")

    def fake_invoke(self, prompt: str, model: str | None = None, timeout: float | None = None) -> str:
        calls.append((self.command, prompt))
        return _analysis_response("fallback")

    monkeypatch.setattr(factory.shutil, "which", lambda _binary: "/usr/bin/codex")
    monkeypatch.setattr(CodexAppServerAiCommand, "from_config", staticmethod(fake_from_config))
    monkeypatch.setattr(SubprocessAiCommand, "invoke", fake_invoke)
    config = {
        "ai_commands": {"repair_analyze": "mock-repair-ai"},
        "codex_app_server": {"enabled": True, "transport": "stdio", "fallback": "subprocess"},
    }

    with caplog.at_level("WARNING"):
        result = LlmRepairEngine(project_root=tmp_path, config=config).analyze(_failure(), _dag())

    assert result.probable_cause == "fallback"
    assert calls[0][0] == "mock-repair-ai"
    assert "Codex App Server fallback" in caplog.text
    assert "app server unavailable" in caplog.text


def test_repair_engine_injected_string_ai_command_uses_factory_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_invoke(self, prompt: str, model: str | None = None, timeout: float | None = None) -> str:
        calls.append((self.command, self.project_root, self.config, prompt))
        return _analysis_response("injected-subprocess")

    monkeypatch.setattr(SubprocessAiCommand, "invoke", fake_invoke)
    config = {"codex_app_server": {"enabled": False}}
    engine = LlmRepairEngine(
        project_root=tmp_path,
        config=config,
        ai_command={"repair_analyze": "mock-injected-ai"},
    )

    result = engine.analyze(_failure(), _dag())

    assert result.probable_cause == "injected-subprocess"
    assert calls[0][0] == "mock-injected-ai"
    assert calls[0][1] == tmp_path
    assert calls[0][2] == config
