"""CDP browser verification template."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from codd.config import load_project_config
from codd.deployment.providers import (
    VerificationResult,
    VerificationTemplate,
    register_verification_template,
)
from codd.deployment.providers.verification.assertion_handlers import ASSERTION_HANDLERS
from codd.deployment.providers.verification.cdp_engines import BROWSER_ENGINES
from codd.deployment.providers.verification.cdp_launchers import CDP_LAUNCHERS
from codd.deployment.providers.verification.cdp_wire import CdpWire, CdpWireError
from codd.deployment.providers.verification.form_strategies import FORM_STRATEGIES


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
WireFactory = Callable[[], CdpWire]


def _runtime_value(runtime_state: Any, name: str, default: Any = None) -> Any:
    return getattr(runtime_state, name, default)


def _optional_runtime_value(runtime_state: Any, name: str) -> Any:
    return getattr(runtime_state, name) if hasattr(runtime_state, name) else None


@register_verification_template("cdp_browser")
class CdpBrowser(VerificationTemplate):
    """Execute declarative journeys through a CDP browser session."""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        wire_factory: WireFactory | None = None,
        run_command: RunCommand | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._config = dict(config) if config is not None else None
        self._wire_factory = wire_factory or CdpWire
        self._run_command = run_command or subprocess.run
        self._sleep = sleep or time.sleep

    def generate_test_command(self, runtime_state: Any, test_kind: str) -> str:
        plan = {
            "template": "cdp_browser",
            "test_kind": test_kind.lower(),
            "target": _runtime_value(runtime_state, "target", ""),
            "identifier": _runtime_value(runtime_state, "identifier", ""),
            "journey": _runtime_value(runtime_state, "journey", None),
            "steps": _runtime_value(runtime_state, "steps", []),
        }
        project_root = _optional_runtime_value(runtime_state, "project_root")
        if project_root is not None:
            plan["project_root"] = str(project_root)
        config = _optional_runtime_value(runtime_state, "cdp_browser_config")
        if config is not None:
            plan["config"] = config
        return json.dumps(plan, sort_keys=True)

    def execute(self, command: str) -> VerificationResult:
        started_at = time.monotonic()
        wire: CdpWire | None = None
        result: VerificationResult | None = None
        teardown_warning: str | None = None
        launcher = None
        launcher_context: dict[str, Any] = {}
        timeout_seconds = 60.0

        try:
            plan = _parse_plan(command)
            config = self._resolve_config(plan)
            timeout_seconds = _float_config(config, "timeout_seconds", 60.0)
            step_timeout = _float_config(config, "step_timeout_seconds", timeout_seconds)

            browser_config = _mapping(config.get("browser"))
            launcher_config = _mapping(config.get("launcher"))
            strategy_config = _mapping(config.get("form_strategy"))

            engine = _build_plugin(
                BROWSER_ENGINES,
                _plugin_name(browser_config, "engine"),
                "browser engine",
            )
            launcher = _build_plugin(
                CDP_LAUNCHERS,
                _plugin_name(launcher_config, "kind"),
                "CDP launcher",
            )
            form_strategy = _build_plugin(
                FORM_STRATEGIES,
                _plugin_name(strategy_config, "kind"),
                "form strategy",
            )

            launcher_context = _plugin_context(config, browser_config, launcher_config, strategy_config)
            launch_result = self._launch(launcher, launcher_context, timeout_seconds)
            if launch_result is not None:
                result = launch_result
                return result

            endpoint = engine.cdp_endpoint(_plugin_context(config, browser_config))
            wire = self._wire_factory()
            connect_result = self._connect_with_retry(wire, endpoint, timeout_seconds)
            if connect_result is not None:
                result = connect_result
                return result

            steps = _steps(plan)
            for index, step in enumerate(steps, start=1):
                step_result = self._dispatch_step(wire, form_strategy, index, step, step_timeout)
                if step_result is not None:
                    result = step_result
                    return result

            output = f"executed {len(steps)} CDP journey step(s)"
            result = VerificationResult(True, output, time.monotonic() - started_at)
            return result
        except Exception as exc:
            result = VerificationResult(False, str(exc), time.monotonic() - started_at)
            return result
        finally:
            if wire is not None:
                wire.close()
            if launcher is not None:
                teardown_warning = self._teardown(launcher, timeout_seconds)
            if result is not None:
                result.duration = time.monotonic() - started_at
                if teardown_warning:
                    result.output = _append_output(result.output, teardown_warning)

    def _launch(
        self,
        launcher: Any,
        launcher_context: Mapping[str, Any],
        timeout_seconds: float,
    ) -> VerificationResult | None:
        try:
            command = launcher.launch_command(launcher_context)
        except Exception as exc:
            return VerificationResult(False, f"launcher command failed: {exc}")

        if not command:
            return None
        completed = self._run_command(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if completed.returncode == 0:
            return None
        output = completed.stderr or completed.stdout or f"launcher exited {completed.returncode}"
        return VerificationResult(False, f"launcher stderr: {output}")

    def _connect_with_retry(
        self,
        wire: CdpWire,
        endpoint: str,
        timeout_seconds: float,
    ) -> VerificationResult | None:
        deadline = time.monotonic() + timeout_seconds
        last_error: str | None = None
        while time.monotonic() <= deadline:
            try:
                wire.connect(endpoint, timeout=min(1.0, max(0.1, deadline - time.monotonic())))
                return None
            except CdpWireError as exc:
                last_error = str(exc)
                self._sleep(0.1)
        suffix = f": {last_error}" if last_error else ""
        return VerificationResult(False, f"CDP connect timeout{suffix}")

    def _dispatch_step(
        self,
        wire: CdpWire,
        form_strategy: Any,
        index: int,
        step: Mapping[str, Any],
        timeout: float,
    ) -> VerificationResult | None:
        action = str(step.get("action", "")).strip()
        if not action:
            return VerificationResult(False, f"step {index} failed: action is required")
        if action in {"evaluate", "script", "javascript", "runtime_evaluate"}:
            return VerificationResult(False, f"step {index} failed: direct script action is not allowed")

        try:
            if action == "navigate":
                target = str(step.get("target") or step.get("url") or "")
                if not target:
                    return VerificationResult(False, f"step {index} failed: navigate target is required")
                wire.send_command("Page.navigate", {"url": target}, timeout=timeout)
                return None
            if action == "click":
                selector = _required_step_value(step, "selector")
                expression = form_strategy.click_js(selector)
                wire.send_command("Runtime.evaluate", {"expression": expression}, timeout=timeout)
                return None
            if action == "fill":
                selector = _required_step_value(step, "selector")
                value = str(step.get("value", ""))
                expression = form_strategy.fill_input_js(selector, value)
                wire.send_command("Runtime.evaluate", {"expression": expression}, timeout=timeout)
                return None
            if action in {"form_submit", "submit"}:
                selector = step.get("selector")
                expression = form_strategy.submit_form_js(str(selector) if selector is not None else None)
                wire.send_command("Runtime.evaluate", {"expression": expression}, timeout=timeout)
                return None
            if action.startswith("expect_"):
                handler_cls = ASSERTION_HANDLERS.get(action)
                if handler_cls is None:
                    return VerificationResult(False, f"step {index} failed: assertion handler not registered: {action}")
                assertion = handler_cls().assert_(wire, step)
                if assertion.passed:
                    return None
                return VerificationResult(False, f"step {index} failed: {assertion.output}")
        except Exception as exc:
            return VerificationResult(False, f"step {index} failed: {exc}")

        return VerificationResult(False, f"step {index} failed: unsupported action: {action}")

    def _teardown(self, launcher: Any, timeout_seconds: float) -> str | None:
        try:
            command = launcher.teardown_command()
        except Exception as exc:
            return f"WARN: teardown failed: {exc}"
        if not command:
            return None
        try:
            completed = self._run_command(
                list(command),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except Exception as exc:
            return f"WARN: teardown failed: {exc}"
        if completed.returncode == 0:
            return None
        output = completed.stderr or completed.stdout or f"teardown exited {completed.returncode}"
        return f"WARN: teardown failed: {output}"

    def _resolve_config(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        if self._config is not None:
            return dict(self._config)
        inline_config = plan.get("config")
        if isinstance(inline_config, Mapping):
            return dict(inline_config)

        project_root = Path(str(plan.get("project_root") or Path.cwd()))
        loaded = load_project_config(project_root)
        cdp_config = (
            loaded.get("verification", {})
            .get("templates", {})
            .get("cdp_browser")
        )
        if not isinstance(cdp_config, Mapping):
            raise ValueError("cdp_browser config not found")
        return dict(cdp_config)


def _parse_plan(command: str) -> dict[str, Any]:
    try:
        plan = json.loads(command)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid CDP journey plan: {exc}") from exc
    if not isinstance(plan, dict):
        raise ValueError("invalid CDP journey plan: root must be an object")
    return plan


def _steps(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("journey steps must be a list")
    normalized: list[Mapping[str, Any]] = []
    for step in steps:
        if not isinstance(step, Mapping):
            raise ValueError("journey step must be an object")
        normalized.append(step)
    return normalized


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _plugin_name(config: Mapping[str, Any], primary_key: str) -> str:
    value = config.get(primary_key) or config.get("name")
    return str(value or "")


def _build_plugin(registry: Mapping[str, type], name: str, label: str) -> Any:
    if not name:
        raise ValueError(f"{label} is not configured")
    plugin_cls = registry.get(name)
    if plugin_cls is None:
        raise ValueError(f"register required plugins: missing {label} '{name}'")
    return plugin_cls()


def _plugin_context(
    template_config: Mapping[str, Any],
    browser_config: Mapping[str, Any],
    launcher_config: Mapping[str, Any] | None = None,
    strategy_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "template": dict(template_config),
        "browser": dict(browser_config),
    }
    context.update(browser_config)
    if launcher_config is not None:
        context["launcher"] = dict(launcher_config)
        context.update(launcher_config)
    if strategy_config is not None:
        context["form_strategy"] = dict(strategy_config)
    return context


def _float_config(config: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _required_step_value(step: Mapping[str, Any], key: str) -> str:
    value = step.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} is required")
    return str(value)


def _append_output(output: str, addition: str) -> str:
    return f"{output}\n{addition}" if output else addition


__all__ = ["CdpBrowser"]
