from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codd.deployment.providers.verification.assertion_handlers import (
    ASSERTION_HANDLERS,
    AssertionHandler,
    AssertionResult,
    register_assertion_handler,
)
from codd.deployment.providers.verification.cdp_browser import CdpBrowser
from codd.deployment.providers.verification.cdp_engines import (
    BROWSER_ENGINES,
    BrowserEngine,
    register_browser_engine,
)
from codd.deployment.providers.verification.cdp_launchers import (
    CDP_LAUNCHERS,
    CdpLauncher,
    register_cdp_launcher,
)
from codd.deployment.providers.verification.cdp_wire import (
    CdpWire,
    CdpWireConnectionError,
    CdpWireProtocolError,
    CdpWireTimeout,
)
from codd.deployment.providers.verification.form_strategies import (
    FORM_STRATEGIES,
    FormInteractionStrategy,
    register_form_strategy,
)


class FakeSocket:
    def __init__(self, responses: list[object] | None = None) -> None:
        self.responses = list(responses or [])
        self.sent: list[dict] = []
        self.timeouts: list[float | None] = []
        self.closed = False

    def settimeout(self, timeout: float | None) -> None:
        self.timeouts.append(timeout)

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self) -> str:
        if not self.responses:
            raise TimeoutError("empty")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return str(response)

    def close(self) -> None:
        self.closed = True


class RecordingWire:
    def __init__(
        self,
        connect_errors: list[Exception] | None = None,
        send_errors: dict[int, Exception] | None = None,
    ) -> None:
        self.connect_errors = list(connect_errors or [])
        self.send_errors = dict(send_errors or {})
        self.connect_calls: list[tuple[str, float | None]] = []
        self.commands: list[tuple[str, dict, float | None]] = []
        self.closed = False

    def connect(self, endpoint: str, timeout: float | None = None) -> None:
        self.connect_calls.append((endpoint, timeout))
        if self.connect_errors:
            raise self.connect_errors.pop(0)

    def send_command(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        call_number = len(self.commands) + 1
        if call_number in self.send_errors:
            raise self.send_errors[call_number]
        payload = dict(params or {})
        self.commands.append((method, payload, timeout))
        return {"ok": True}

    def close(self) -> None:
        self.closed = True


class CommandRunner:
    def __init__(self, results: list[subprocess.CompletedProcess[str]] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), kwargs))
        if self.results:
            return self.results.pop(0)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


@pytest.fixture(autouse=True)
def restore_registries():
    engines = dict(BROWSER_ENGINES)
    launchers = dict(CDP_LAUNCHERS)
    strategies = dict(FORM_STRATEGIES)
    assertions = dict(ASSERTION_HANDLERS)
    yield
    BROWSER_ENGINES.clear()
    BROWSER_ENGINES.update(engines)
    CDP_LAUNCHERS.clear()
    CDP_LAUNCHERS.update(launchers)
    FORM_STRATEGIES.clear()
    FORM_STRATEGIES.update(strategies)
    ASSERTION_HANDLERS.clear()
    ASSERTION_HANDLERS.update(assertions)


def _socket_factory(fake_socket: FakeSocket):
    return lambda endpoint, timeout: fake_socket


def _config(timeout: float = 2.0) -> dict:
    return {
        "browser": {"engine": "mock", "endpoint": "ws://cdp.test/session"},
        "launcher": {"kind": "mock"},
        "form_strategy": {"kind": "mock"},
        "timeout_seconds": timeout,
        "step_timeout_seconds": 0.5,
    }


def _register_mock_plugins(
    launch_command: list[str] | None = None,
    teardown_command: list[str] | None = None,
    assertion_result: AssertionResult | None = None,
) -> None:
    launch = ["launch"] if launch_command is None else launch_command
    teardown = ["stop"] if teardown_command is None else teardown_command
    assertion = assertion_result or AssertionResult(True, "ok")

    @register_browser_engine("mock")
    class MockEngine(BrowserEngine):
        engine_name = "mock"

        def cdp_endpoint(self, config):
            return str(config["endpoint"])

        def normalized_capabilities(self):
            return {"journey"}

    @register_cdp_launcher("mock")
    class MockLauncher(CdpLauncher):
        launcher_name = "mock"

        def launch_command(self, browser_config):
            return list(launch)

        def teardown_command(self):
            return list(teardown)

        def is_alive(self, browser_config):
            return True

    @register_form_strategy("mock")
    class MockStrategy(FormInteractionStrategy):
        strategy_name = "mock"

        def fill_input_js(self, selector: str, value: str) -> str:
            return f"fill:{selector}:{value}"

        def click_js(self, selector: str) -> str:
            return f"click:{selector}"

        def submit_form_js(self, selector: str | None = None) -> str:
            return f"submit:{selector or ''}"

    @register_assertion_handler("expect_url")
    class MockAssertion(AssertionHandler):
        action_name = "expect_url"

        def assert_(self, cdp_session, step):
            return assertion


def test_cdp_wire_connect_uses_socket_factory():
    fake_socket = FakeSocket()
    wire = CdpWire(socket_factory=_socket_factory(fake_socket))

    wire.connect("ws://cdp.test", timeout=3)

    assert wire.connected is True


def test_cdp_wire_send_command_returns_result_and_sets_timeout():
    fake_socket = FakeSocket([json.dumps({"id": 1, "result": {"frameId": "abc"}})])
    wire = CdpWire(socket_factory=_socket_factory(fake_socket))
    wire.connect("ws://cdp.test")

    result = wire.send_command("Page.navigate", {"url": "https://example.test"}, timeout=4)

    assert result == {"frameId": "abc"}
    assert fake_socket.sent == [
        {"id": 1, "method": "Page.navigate", "params": {"url": "https://example.test"}}
    ]
    assert fake_socket.timeouts == [4]


def test_cdp_wire_increments_request_ids():
    fake_socket = FakeSocket([
        json.dumps({"id": 1, "result": {}}),
        json.dumps({"id": 2, "result": {}}),
    ])
    wire = CdpWire(socket_factory=_socket_factory(fake_socket))
    wire.connect("ws://cdp.test")

    wire.send_command("First.command")
    wire.send_command("Second.command")

    assert [payload["id"] for payload in fake_socket.sent] == [1, 2]


def test_cdp_wire_ignores_events_until_matching_response():
    fake_socket = FakeSocket([
        json.dumps({"method": "Page.loadEventFired", "params": {}}),
        json.dumps({"id": 99, "result": {}}),
        json.dumps({"id": 1, "result": {"done": True}}),
    ])
    wire = CdpWire(socket_factory=_socket_factory(fake_socket))
    wire.connect("ws://cdp.test")

    assert wire.send_command("Runtime.evaluate") == {"done": True}


def test_cdp_wire_error_response_raises_protocol_error():
    fake_socket = FakeSocket([json.dumps({"id": 1, "error": {"message": "bad"}})])
    wire = CdpWire(socket_factory=_socket_factory(fake_socket))
    wire.connect("ws://cdp.test")

    with pytest.raises(CdpWireProtocolError):
        wire.send_command("Runtime.evaluate")


def test_cdp_wire_timeout_maps_to_timeout():
    fake_socket = FakeSocket([TimeoutError("late")])
    wire = CdpWire(socket_factory=_socket_factory(fake_socket))
    wire.connect("ws://cdp.test")

    with pytest.raises(CdpWireTimeout):
        wire.send_command("Runtime.evaluate")


def test_cdp_wire_requires_connection():
    with pytest.raises(CdpWireConnectionError):
        CdpWire(socket_factory=_socket_factory(FakeSocket())).send_command("Runtime.evaluate")


def test_cdp_wire_close_closes_socket():
    fake_socket = FakeSocket()
    wire = CdpWire(socket_factory=_socket_factory(fake_socket))
    wire.connect("ws://cdp.test")

    wire.close()

    assert fake_socket.closed is True
    assert wire.connected is False


def test_cdp_wire_invalid_response_json_raises_protocol_error():
    fake_socket = FakeSocket(["not-json"])
    wire = CdpWire(socket_factory=_socket_factory(fake_socket))
    wire.connect("ws://cdp.test")

    with pytest.raises(CdpWireProtocolError):
        wire.send_command("Runtime.evaluate")


def test_cdp_browser_execute_dispatches_full_journey():
    _register_mock_plugins()
    wire = RecordingWire()
    runner = CommandRunner()
    plan = {
        "steps": [
            {"action": "navigate", "target": "https://example.test/login"},
            {"action": "click", "selector": "#start"},
            {"action": "fill", "selector": "#email", "value": "user@example.test"},
            {"action": "form_submit", "selector": "form"},
            {"action": "expect_url", "contains": "/dashboard"},
        ]
    }

    result = CdpBrowser(
        config=_config(),
        wire_factory=lambda: wire,
        run_command=runner,
        sleep=lambda _: None,
    ).execute(json.dumps(plan))

    assert result.passed is True
    assert "executed 5" in result.output
    assert wire.connect_calls[0][0] == "ws://cdp.test/session"
    assert wire.commands == [
        ("Page.navigate", {"url": "https://example.test/login"}, 0.5),
        ("Runtime.evaluate", {"expression": "click:#start"}, 0.5),
        ("Runtime.evaluate", {"expression": "fill:#email:user@example.test"}, 0.5),
        ("Runtime.evaluate", {"expression": "submit:form"}, 0.5),
    ]
    assert [call[0] for call in runner.calls] == [["launch"], ["stop"]]
    assert wire.closed is True


def test_cdp_browser_loads_project_config_from_codd_yaml(tmp_path: Path):
    _register_mock_plugins(launch_command=[], teardown_command=[])
    wire = RecordingWire()
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        "verification:\n"
        "  templates:\n"
        "    cdp_browser:\n"
        "      browser:\n"
        "        engine: mock\n"
        "        endpoint: ws://cdp.test/from-config\n"
        "      launcher:\n"
        "        kind: mock\n"
        "      form_strategy:\n"
        "        kind: mock\n"
        "      timeout_seconds: 1\n",
        encoding="utf-8",
    )

    result = CdpBrowser(wire_factory=lambda: wire, sleep=lambda _: None).execute(
        json.dumps({"project_root": str(tmp_path), "steps": []})
    )

    assert result.passed is True
    assert wire.connect_calls[0][0] == "ws://cdp.test/from-config"


def test_cdp_browser_missing_config_returns_failure(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = CdpBrowser().execute(json.dumps({"steps": []}))

    assert result.passed is False
    assert "config" in result.output


def test_cdp_browser_missing_plugin_returns_failure():
    result = CdpBrowser(config=_config()).execute(json.dumps({"steps": []}))

    assert result.passed is False
    assert "missing browser engine" in result.output


def test_cdp_browser_launcher_failure_returns_stderr():
    _register_mock_plugins()
    runner = CommandRunner([subprocess.CompletedProcess(["launch"], 2, stdout="", stderr="no start")])

    result = CdpBrowser(
        config=_config(),
        wire_factory=lambda: RecordingWire(),
        run_command=runner,
        sleep=lambda _: None,
    ).execute(json.dumps({"steps": []}))

    assert result.passed is False
    assert "launcher stderr: no start" in result.output


def test_cdp_browser_connect_timeout_returns_failure():
    _register_mock_plugins(launch_command=[], teardown_command=[])

    class AlwaysFailWire(RecordingWire):
        def connect(self, endpoint: str, timeout: float | None = None) -> None:
            self.connect_calls.append((endpoint, timeout))
            raise CdpWireConnectionError("not ready")

    wire = AlwaysFailWire()

    result = CdpBrowser(
        config=_config(timeout=0.01),
        wire_factory=lambda: wire,
        sleep=lambda _: None,
    ).execute(json.dumps({"steps": []}))

    assert result.passed is False
    assert "CDP connect timeout" in result.output


def test_cdp_browser_step_failure_includes_index():
    _register_mock_plugins(launch_command=[], teardown_command=[])
    wire = RecordingWire(send_errors={2: RuntimeError("boom")})
    plan = {
        "steps": [
            {"action": "navigate", "target": "https://example.test/a"},
            {"action": "click", "selector": "#start"},
        ]
    }

    result = CdpBrowser(config=_config(), wire_factory=lambda: wire, sleep=lambda _: None).execute(
        json.dumps(plan)
    )

    assert result.passed is False
    assert "step 2 failed: boom" in result.output


def test_cdp_browser_assertion_failure_returns_failed_result():
    _register_mock_plugins(
        launch_command=[],
        teardown_command=[],
        assertion_result=AssertionResult(False, "url mismatch"),
    )

    result = CdpBrowser(config=_config(), wire_factory=lambda: RecordingWire(), sleep=lambda _: None).execute(
        json.dumps({"steps": [{"action": "expect_url"}]})
    )

    assert result.passed is False
    assert "step 1 failed: url mismatch" in result.output


def test_cdp_browser_missing_assertion_handler_returns_failure():
    _register_mock_plugins(launch_command=[], teardown_command=[])

    result = CdpBrowser(config=_config(), wire_factory=lambda: RecordingWire(), sleep=lambda _: None).execute(
        json.dumps({"steps": [{"action": "expect_ready"}]})
    )

    assert result.passed is False
    assert "assertion handler not registered" in result.output


def test_cdp_browser_teardown_failure_keeps_result_and_warns():
    _register_mock_plugins()
    runner = CommandRunner([
        subprocess.CompletedProcess(["launch"], 0, stdout="ok", stderr=""),
        subprocess.CompletedProcess(["stop"], 3, stdout="", stderr="stop failed"),
    ])

    result = CdpBrowser(
        config=_config(),
        wire_factory=lambda: RecordingWire(),
        run_command=runner,
        sleep=lambda _: None,
    ).execute(json.dumps({"steps": []}))

    assert result.passed is True
    assert "WARN: teardown failed: stop failed" in result.output


def test_cdp_browser_rejects_direct_script_action():
    _register_mock_plugins(launch_command=[], teardown_command=[])
    wire = RecordingWire()

    result = CdpBrowser(config=_config(), wire_factory=lambda: wire, sleep=lambda _: None).execute(
        json.dumps({"steps": [{"action": "evaluate", "expression": "1 + 1"}]})
    )

    assert result.passed is False
    assert "direct script action is not allowed" in result.output
    assert wire.commands == []


def test_cdp_browser_invalid_json_returns_failure():
    result = CdpBrowser(config=_config()).execute("{")

    assert result.passed is False
    assert "invalid CDP journey plan" in result.output


def test_cdp_browser_generate_test_command_includes_optional_runtime_data(tmp_path: Path):
    class RuntimeState:
        target = "https://example.test"
        identifier = "runtime:server:app"
        journey = "login"
        steps = [{"action": "navigate", "target": "https://example.test"}]
        project_root = tmp_path
        cdp_browser_config = _config()

    plan = json.loads(CdpBrowser().generate_test_command(RuntimeState(), "E2E"))

    assert plan["project_root"] == str(tmp_path)
    assert plan["config"]["browser"]["engine"] == "mock"
    assert plan["steps"] == [{"action": "navigate", "target": "https://example.test"}]


def test_cdp_browser_step_list_must_be_array():
    _register_mock_plugins(launch_command=[], teardown_command=[])

    result = CdpBrowser(config=_config(), wire_factory=lambda: RecordingWire(), sleep=lambda _: None).execute(
        json.dumps({"steps": {"action": "navigate"}})
    )

    assert result.passed is False
    assert "journey steps must be a list" in result.output


def test_cdp_browser_submit_alias_dispatches_submit_form():
    _register_mock_plugins(launch_command=[], teardown_command=[])
    wire = RecordingWire()

    result = CdpBrowser(config=_config(), wire_factory=lambda: wire, sleep=lambda _: None).execute(
        json.dumps({"steps": [{"action": "submit"}]})
    )

    assert result.passed is True
    assert wire.commands == [("Runtime.evaluate", {"expression": "submit:"}, 0.5)]


def test_cdp_browser_empty_launch_command_skips_launch_subprocess():
    _register_mock_plugins(launch_command=[], teardown_command=[])
    runner = CommandRunner()

    result = CdpBrowser(
        config=_config(),
        wire_factory=lambda: RecordingWire(),
        run_command=runner,
        sleep=lambda _: None,
    ).execute(json.dumps({"steps": []}))

    assert result.passed is True
    assert runner.calls == []


def test_cdp_browser_form_step_requires_selector():
    _register_mock_plugins(launch_command=[], teardown_command=[])

    result = CdpBrowser(config=_config(), wire_factory=lambda: RecordingWire(), sleep=lambda _: None).execute(
        json.dumps({"steps": [{"action": "click"}]})
    )

    assert result.passed is False
    assert "selector is required" in result.output


def test_cdp_browser_unsupported_action_returns_failure():
    _register_mock_plugins(launch_command=[], teardown_command=[])

    result = CdpBrowser(config=_config(), wire_factory=lambda: RecordingWire(), sleep=lambda _: None).execute(
        json.dumps({"steps": [{"action": "hover", "selector": "#x"}]})
    )

    assert result.passed is False
    assert "unsupported action" in result.output
