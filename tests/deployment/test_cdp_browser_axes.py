from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.deployment.providers import VerificationResult
from codd.deployment.providers.verification.assertion_handlers import ASSERTION_HANDLERS
from codd.deployment.providers.verification.cdp_browser import CdpBrowser
from codd.deployment.providers.verification.cdp_engines import (
    BROWSER_ENGINES,
    BrowserEngine,
    runtime_commands_for_attributes,
    register_browser_engine,
)
from codd.deployment.providers.verification.cdp_launchers import (
    CDP_LAUNCHERS,
    CdpLauncher,
    register_cdp_launcher,
)
from codd.deployment.providers.verification.form_strategies import (
    FORM_STRATEGIES,
    FormInteractionStrategy,
    register_form_strategy,
)
import codd.deployment.providers.verification.cdp_browser as cdp_browser_module


class RecordingWire:
    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, float | None]] = []
        self.commands: list[tuple[str, dict[str, Any], float | None]] = []
        self.closed = False

    def connect(self, endpoint: str, timeout: float | None = None) -> None:
        self.connect_calls.append((endpoint, timeout))

    def send_command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.commands.append((method, dict(params or {}), timeout))
        return {"ok": True}

    def close(self) -> None:
        self.closed = True


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


def _config() -> dict[str, Any]:
    return {
        "browser": {"engine": "mock", "endpoint": "ws://cdp.test/session"},
        "launcher": {"kind": "mock"},
        "form_strategy": {"kind": "mock"},
        "timeout_seconds": 1,
        "step_timeout_seconds": 0.25,
    }


def _register_mock_plugins() -> None:
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
            return []

        def teardown_command(self):
            return []

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


def _write_project(
    project_root: Path,
    *,
    templates: dict[str, Any] | None = None,
    journeys: list[dict[str, Any]] | None = None,
    axes: list[dict[str, Any]] | None = None,
) -> None:
    config: dict[str, Any] = {
        "project": {"type": "generic"},
        "dag": {
            "design_doc_patterns": ["docs/design/*.md"],
            "impl_file_patterns": [],
            "test_file_patterns": [],
            "plan_task_file": "docs/design/implementation_plan.md",
            "lexicon_file": "project_lexicon.yaml",
        },
    }
    if templates is not None:
        config["verification"] = {"templates": templates}
    (project_root / "codd").mkdir(parents=True, exist_ok=True)
    (project_root / "codd" / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    if journeys is not None:
        frontmatter = yaml.safe_dump({"user_journeys": journeys}, explicit_start=True, sort_keys=False)
        design_dir = project_root / "docs" / "design"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "flow.md").write_text(frontmatter + "---\n# Flow\n", encoding="utf-8")

    if axes is not None:
        (project_root / "project_lexicon.yaml").write_text(
            yaml.safe_dump({"coverage_axes": axes}, sort_keys=False),
            encoding="utf-8",
        )


def _journey(name: str = "primary_flow") -> dict[str, Any]:
    return {
        "name": name,
        "criticality": "critical",
        "steps": [{"action": "navigate", "target": "/start"}],
        "required_capabilities": [],
        "expected_outcome_refs": [],
    }


def test_runtime_attributes_emit_dimension_command():
    commands = runtime_commands_for_attributes({"width": 320, "height": 640, "device_scale_factor": 2})

    assert [(command.method, command.params) for command in commands] == [
        (
            "Emulation.setDeviceMetricsOverride",
            {"width": 320, "height": 640, "deviceScaleFactor": 2.0, "mobile": False},
        )
    ]


def test_runtime_attributes_emit_locale_timezone_and_agent_commands():
    commands = runtime_commands_for_attributes(
        {"locale": "ja-JP", "timezone": "Asia/Tokyo", "user_agent": "TestAgent"}
    )

    assert [(command.method, command.params) for command in commands] == [
        ("Emulation.setLocaleOverride", {"locale": "ja-JP"}),
        ("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Tokyo"}),
        ("Network.setUserAgentOverride", {"userAgent": "TestAgent"}),
    ]


def test_runtime_attributes_accept_explicit_commands():
    commands = runtime_commands_for_attributes(
        {"cdp_commands": [{"method": "Network.enable", "params": {"maxTotalBufferSize": 1024}}]}
    )

    assert [(command.method, command.params) for command in commands] == [
        ("Network.enable", {"maxTotalBufferSize": 1024})
    ]


def test_runtime_attributes_require_dimension_pair():
    with pytest.raises(ValueError, match="width and height"):
        runtime_commands_for_attributes({"width": 320})


def test_runtime_attributes_validate_dimension_type():
    with pytest.raises(ValueError, match="must be an integer"):
        runtime_commands_for_attributes({"width": "wide", "height": 640})


def test_cdp_browser_applies_lexicon_variant_before_steps(tmp_path: Path):
    _register_mock_plugins()
    _write_project(
        tmp_path,
        axes=[
            {
                "axis_type": "surface",
                "variants": [{"id": "compact", "attributes": {"width": 320, "height": 640}}],
            }
        ],
    )
    wire = RecordingWire()
    plan = {
        "project_root": str(tmp_path),
        "axis_overrides": {"surface": "compact"},
        "steps": [{"action": "navigate", "target": "https://example.test/start"}],
    }

    result = CdpBrowser(config=_config(), wire_factory=lambda: wire, sleep=lambda _: None).execute(json.dumps(plan))

    assert result.passed is True
    assert [command[0] for command in wire.commands] == [
        "Emulation.setDeviceMetricsOverride",
        "Page.navigate",
    ]
    assert wire.commands[0][1]["width"] == 320


def test_cdp_browser_execute_argument_overrides_plan_axes(tmp_path: Path):
    _register_mock_plugins()
    _write_project(
        tmp_path,
        axes=[
            {
                "axis_type": "surface",
                "variants": [
                    {"id": "compact", "attributes": {"width": 320, "height": 640}},
                    {"id": "expanded", "attributes": {"width": 1200, "height": 800}},
                ],
            }
        ],
    )
    wire = RecordingWire()
    plan = {"project_root": str(tmp_path), "axis_overrides": {"surface": "compact"}, "steps": []}

    result = CdpBrowser(config=_config(), wire_factory=lambda: wire, sleep=lambda _: None).execute(
        json.dumps(plan),
        axis_overrides={"surface": "expanded"},
    )

    assert result.passed is True
    assert wire.commands[0][1]["width"] == 1200


def test_cdp_browser_accepts_inline_axis_attributes(tmp_path: Path):
    _register_mock_plugins()
    wire = RecordingWire()
    plan = {
        "project_root": str(tmp_path),
        "axis_overrides": {"surface": "compact"},
        "axis_attributes": {"surface": {"compact": {"locale": "ja-JP"}}},
        "steps": [],
    }

    result = CdpBrowser(config=_config(), wire_factory=lambda: wire, sleep=lambda _: None).execute(json.dumps(plan))

    assert result.passed is True
    assert wire.commands == [("Emulation.setLocaleOverride", {"locale": "ja-JP"}, 0.25)]


def test_cdp_browser_missing_axis_variant_fails(tmp_path: Path):
    _register_mock_plugins()
    _write_project(tmp_path, axes=[{"axis_type": "surface", "variants": []}])

    result = CdpBrowser(config=_config(), wire_factory=RecordingWire, sleep=lambda _: None).execute(
        json.dumps({"project_root": str(tmp_path), "axis_overrides": {"surface": "missing"}, "steps": []})
    )

    assert result.passed is False
    assert "axis variant not found: surface=missing" in result.output


def test_cdp_browser_generate_command_includes_runtime_axis_overrides(tmp_path: Path):
    class RuntimeState:
        target = "https://example.test"
        identifier = "runtime:app"
        journey = "login"
        steps = []
        project_root = tmp_path
        cdp_browser_config = _config()
        axis_overrides = {"surface": "compact"}

    plan = json.loads(CdpBrowser().generate_test_command(RuntimeState(), "E2E"))

    assert plan["axis_overrides"] == {"surface": "compact"}


def test_run_journey_axis_option_reaches_browser_plan(tmp_path: Path, monkeypatch):
    calls: list[dict[str, Any]] = []

    class FakeCdpBrowser:
        def __init__(self, config=None):
            self.config = config

        def execute(self, command: str) -> VerificationResult:
            calls.append({"config": self.config, "command": json.loads(command)})
            return VerificationResult(True, "journey ok")

    monkeypatch.setattr(cdp_browser_module, "CdpBrowser", FakeCdpBrowser)
    _write_project(
        tmp_path,
        templates={"cdp_browser": {"browser": {"engine": "mock"}}},
        journeys=[_journey()],
    )

    result = CliRunner().invoke(
        main,
        ["dag", "run-journey", "primary_flow", "--path", str(tmp_path), "--axis", "surface=compact"],
    )

    assert result.exit_code == 0
    assert calls[0]["command"]["axis_overrides"] == {"surface": "compact"}


def test_run_journey_rejects_invalid_axis_flag(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cdp_browser_module, "CdpBrowser", object)
    _write_project(
        tmp_path,
        templates={"cdp_browser": {"browser": {"engine": "mock"}}},
        journeys=[_journey()],
    )

    result = CliRunner().invoke(
        main,
        ["dag", "run-journey", "primary_flow", "--path", str(tmp_path), "--axis", "surface"],
    )

    assert result.exit_code == 2
    assert "--axis must use TYPE=VARIANT" in result.output


def test_cdp_axis_core_generality_gate_zero_hit():
    root = Path(__file__).resolve().parents[2]
    text = "\n".join(
        [
            (root / "codd" / "deployment" / "providers" / "verification" / "cdp_browser.py").read_text(
                encoding="utf-8"
            ),
            (root / "codd" / "deployment" / "providers" / "verification" / "cdp_engines.py").read_text(
                encoding="utf-8"
            ),
        ]
    )
    forbidden = re.compile(
        r"page\.tsx|route\.ts|next\.?js|react|django|rails|smartphone|iphone|android|375|1920|web app|mobile app|cli|backend|embedded",
        re.IGNORECASE,
    )

    assert forbidden.search(text) is None
