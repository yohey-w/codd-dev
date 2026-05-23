"""Runtime smoke check implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from time import perf_counter
import time
from typing import Any
from urllib.parse import urljoin
import subprocess

import httpx

from codd.runtime_smoke.config import (
    ActionOutcomeTargetConfig,
    ConnectivityConfig,
    CrudFlowTargetConfig,
    DbCheckConfig,
    DevServerConfig,
    E2eConfig,
)


@dataclass
class CheckResult:
    passed: bool
    name: str
    output: str
    elapsed_sec: float
    category: str = ""
    skipped: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class DbChecker:
    def __init__(self, config: DbCheckConfig, project_root: Path):
        self.config = config
        self.project_root = project_root

    def run(self) -> CheckResult:
        if not self.config.command:
            return _missing_config("db", "runtime_smoke.db_check.command is not configured")

        started = perf_counter()
        try:
            completed = subprocess.run(
                self.config.command,
                shell=True,
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name="DB up",
                category="db",
                output=_join_output(str(exc.stdout or ""), str(exc.stderr or ""), f"timeout after {self.config.timeout}s"),
                elapsed_sec=elapsed,
                details={"command": self.config.command, "timeout": self.config.timeout},
            )

        elapsed = perf_counter() - started
        passed = completed.returncode == self.config.expected_exit_code
        return CheckResult(
            passed=passed,
            name="DB up",
            category="db",
            output=_join_output(
                completed.stdout,
                completed.stderr,
                f"exit_code={completed.returncode}, expected={self.config.expected_exit_code}",
            ),
            elapsed_sec=elapsed,
            details={"command": self.config.command, "exit_code": completed.returncode},
        )


class DevServerChecker:
    def __init__(self, config: DevServerConfig):
        self.config = config

    def run(self) -> CheckResult:
        if not self.config.url:
            return _missing_config("dev-server", "runtime_smoke.dev_server.url is not configured")

        started = perf_counter()
        try:
            response = httpx.get(self.config.url, timeout=self.config.timeout)
            elapsed = perf_counter() - started
        except httpx.TimeoutException as exc:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name="Dev server up",
                category="dev-server",
                output=f"timeout requesting {self.config.url}: {exc}",
                elapsed_sec=elapsed,
                details={"url": self.config.url, "timeout": self.config.timeout},
            )
        except httpx.RequestError as exc:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name="Dev server up",
                category="dev-server",
                output=f"request error for {self.config.url}: {exc}",
                elapsed_sec=elapsed,
                details={"url": self.config.url},
            )

        passed = response.status_code == self.config.expected_status
        return CheckResult(
            passed=passed,
            name="Dev server up",
            category="dev-server",
            output=f"GET {self.config.url} -> HTTP {response.status_code}, expected {self.config.expected_status}",
            elapsed_sec=elapsed,
            details={"url": self.config.url, "status_code": response.status_code},
        )


class SmokeConnectivityChecker:
    def __init__(self, checks: list[ConnectivityConfig], base_url: str | None):
        self.checks = checks
        self.base_url = base_url

    def run(self) -> list[CheckResult]:
        if not self.checks:
            return [_missing_config("connectivity", "runtime_smoke.smoke_connectivity is not configured")]

        results: list[CheckResult] = []
        cookie_jars: dict[str, httpx.Cookies] = {}
        with httpx.Client(follow_redirects=False) as client:
            for index, check in enumerate(self.checks, start=1):
                results.append(self._run_one(client, cookie_jars, check, index))
        return results

    def _run_one(
        self,
        client: httpx.Client,
        cookie_jars: dict[str, httpx.Cookies],
        check: ConnectivityConfig,
        index: int,
    ) -> CheckResult:
        url = _resolve_url(check.url, self.base_url)
        if check.cookie_jar and check.cookie_jar in cookie_jars:
            client.cookies.update(cookie_jars[check.cookie_jar])

        started = perf_counter()
        try:
            response = client.request(
                check.method,
                url,
                headers=check.headers or None,
                data=check.body if check.body is not None else None,
                json=check.json if check.json is not None else None,
                timeout=check.timeout,
            )
            elapsed = perf_counter() - started
        except httpx.TimeoutException as exc:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name=f"Smoke connectivity: {check.name}",
                category="connectivity",
                output=f"timeout requesting {url}: {exc}",
                elapsed_sec=elapsed,
                details={"method": check.method, "url": url, "timeout": check.timeout},
            )
        except httpx.RequestError as exc:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name=f"Smoke connectivity: {check.name}",
                category="connectivity",
                output=f"request error for {url}: {exc}",
                elapsed_sec=elapsed,
                details={"method": check.method, "url": url},
            )

        response_elapsed = _response_elapsed_seconds(response)
        elapsed_for_gate = response_elapsed if response_elapsed is not None else elapsed
        body = str(getattr(response, "text", "") or "")
        status_ok = response.status_code == check.expected_status
        elapsed_ok = elapsed_for_gate <= check.timeout
        expect_text_ok = check.expect_text is None or check.expect_text in body
        forbid_text_ok = check.forbid_text is None or check.forbid_text not in body
        expect_headers_ok, expect_header_output = _header_assertions(
            response,
            check.expect_headers,
            should_contain=True,
        )
        forbid_headers_ok, forbid_header_output = _header_assertions(
            response,
            check.forbid_headers,
            should_contain=False,
        )
        passed = (
            status_ok
            and elapsed_ok
            and expect_text_ok
            and forbid_text_ok
            and expect_headers_ok
            and forbid_headers_ok
        )
        if check.save_cookie_jar:
            cookie_jars[check.save_cookie_jar] = httpx.Cookies(client.cookies)

        return CheckResult(
            passed=passed,
            name=f"Smoke connectivity: {check.name}",
            category="connectivity",
            output=(
                f"{check.method} {url} -> HTTP {response.status_code}, "
                f"expected {check.expected_status}, elapsed {elapsed_for_gate:.3f}s <= {check.timeout:.3f}s, "
                f"expect_text={check.expect_text!r}, forbid_text={check.forbid_text!r}"
                f"{expect_header_output}{forbid_header_output}"
            ),
            elapsed_sec=elapsed,
            details={
                "index": index,
                "method": check.method,
                "url": url,
                "status_code": response.status_code,
                "response_elapsed_sec": elapsed_for_gate,
                "expect_text": check.expect_text,
                "forbid_text": check.forbid_text,
                "expect_headers": check.expect_headers,
                "forbid_headers": check.forbid_headers,
            },
        )


class E2eChecker:
    def __init__(self, config: E2eConfig, project_root: Path, dev_server_url: str | None):
        self.config = config
        self.project_root = project_root
        self.dev_server_url = dev_server_url

    def run(self) -> CheckResult:
        if not self.config.command:
            return _missing_config("e2e", "runtime_smoke.e2e.command is not configured")

        started = perf_counter()
        try:
            completed = subprocess.run(
                self.config.command,
                shell=True,
                cwd=self._working_dir(),
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name="Real-browser E2E",
                category="e2e",
                output=_join_output(str(exc.stdout or ""), str(exc.stderr or ""), f"timeout after {self.config.timeout}s"),
                elapsed_sec=elapsed,
                details={"command": self.config.command, "timeout": self.config.timeout},
            )

        elapsed = perf_counter() - started
        passed = completed.returncode == 0
        return CheckResult(
            passed=passed,
            name="Real-browser E2E",
            category="e2e",
            output=_join_output(completed.stdout, completed.stderr, f"exit_code={completed.returncode}"),
            elapsed_sec=elapsed,
            details={"command": self.config.command, "exit_code": completed.returncode},
        )

    def _working_dir(self) -> Path:
        if not self.config.working_dir:
            return self.project_root
        path = Path(self.config.working_dir).expanduser()
        if path.is_absolute():
            return path
        return self.project_root / path

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key, value in self.config.env.items():
            env[key] = _render_token(value, self.dev_server_url)
        return env


class CrudFlowChecker:
    def __init__(self, targets: list[CrudFlowTargetConfig], project_root: Path, dev_server_url: str | None):
        self.targets = targets
        self.project_root = project_root
        self.dev_server_url = dev_server_url

    def run(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        with httpx.Client(follow_redirects=False) as client:
            for target in self.targets:
                if target.command:
                    results.append(self._run_command(target))
                else:
                    results.append(self._run_http_flow(client, target))
        return results

    def _run_command(self, target: CrudFlowTargetConfig) -> CheckResult:
        started = perf_counter()
        try:
            completed = subprocess.run(
                target.command,
                shell=True,
                cwd=_working_dir(self.project_root, target.working_dir),
                env=_env(target.env, self.dev_server_url),
                capture_output=True,
                text=True,
                timeout=target.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name=f"CRUD flow: {target.name}",
                category="crud-flow",
                output=_join_output(str(exc.stdout or ""), str(exc.stderr or ""), f"timeout after {target.timeout}s"),
                elapsed_sec=elapsed,
                details={"command": target.command, "timeout": target.timeout},
            )

        elapsed = perf_counter() - started
        return CheckResult(
            passed=completed.returncode == 0,
            name=f"CRUD flow: {target.name}",
            category="crud-flow",
            output=_join_output(completed.stdout, completed.stderr, f"exit_code={completed.returncode}"),
            elapsed_sec=elapsed,
            details={"command": target.command, "exit_code": completed.returncode},
        )

    def _run_http_flow(self, client: httpx.Client, target: CrudFlowTargetConfig) -> CheckResult:
        started = perf_counter()
        create = target.create
        reflect = target.reflect
        if create is None or reflect is None:
            return _missing_config("crud-flow", "runtime.crud_flow_targets requires command or create+reflect")

        create_url = _resolve_url(create.url, self.dev_server_url)
        try:
            create_response = client.request(
                create.method,
                create_url,
                headers=create.headers or None,
                data=create.body if create.body is not None else None,
                json=create.json if create.json is not None else None,
                timeout=create.timeout,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name=f"CRUD flow: {target.name}",
                category="crud-flow",
                output=f"create request failed for {create_url}: {exc}",
                elapsed_sec=elapsed,
                details={"create_url": create_url},
            )

        if create_response.status_code != create.expected_status:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name=f"CRUD flow: {target.name}",
                category="crud-flow",
                output=f"{create.method} {create_url} -> HTTP {create_response.status_code}, expected {create.expected_status}",
                elapsed_sec=elapsed,
                details={"create_url": create_url, "status_code": create_response.status_code},
            )

        reflect_url = _resolve_url(reflect.url, self.dev_server_url)
        attempts = 0
        last_output = ""
        deadline = perf_counter() + target.max_wait_seconds
        while True:
            attempts += 1
            try:
                response = client.request(
                    reflect.method,
                    reflect_url,
                    headers=reflect.headers or None,
                    data=reflect.body if reflect.body is not None else None,
                    json=reflect.json if reflect.json is not None else None,
                    timeout=reflect.timeout,
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last_output = f"reflect request failed for {reflect_url}: {exc}"
            else:
                body = str(getattr(response, "text", "") or "")
                status_ok = response.status_code == reflect.expected_status
                text_ok = target.expect_text is None or target.expect_text in body
                last_output = (
                    f"{reflect.method} {reflect_url} -> HTTP {response.status_code}, "
                    f"expected {reflect.expected_status}, expect_text={target.expect_text!r}, attempt={attempts}"
                )
                if status_ok and text_ok:
                    elapsed = perf_counter() - started
                    return CheckResult(
                        passed=True,
                        name=f"CRUD flow: {target.name}",
                        category="crud-flow",
                        output=f"create OK; reflection OK after {attempts} attempt(s): {last_output}",
                        elapsed_sec=elapsed,
                        details={"create_url": create_url, "reflect_url": reflect_url, "attempts": attempts},
                    )

            if perf_counter() >= deadline:
                elapsed = perf_counter() - started
                return CheckResult(
                    passed=False,
                    name=f"CRUD flow: {target.name}",
                    category="crud-flow",
                    output=f"create OK; reflection not observed within {target.max_wait_seconds:.3f}s: {last_output}",
                    elapsed_sec=elapsed,
                    details={"create_url": create_url, "reflect_url": reflect_url, "attempts": attempts},
                )
            time.sleep(max(0.0, min(target.poll_interval, deadline - perf_counter())))


class ActionOutcomeChecker:
    def __init__(
        self,
        targets: list[ActionOutcomeTargetConfig],
        project_root: Path,
        dev_server_url: str | None,
        *,
        category: str = "action-outcome",
        label: str = "Action outcome",
        missing_config_message: str = "runtime.action_outcome_targets requires command or invoke+observe",
    ):
        self.targets = targets
        self.project_root = project_root
        self.dev_server_url = dev_server_url
        self.category = category
        self.label = label
        self.missing_config_message = missing_config_message

    def run(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        with httpx.Client(follow_redirects=False) as client:
            for target in self.targets:
                if target.command:
                    results.append(self._run_command(target))
                else:
                    results.append(self._run_http_flow(client, target))
        return results

    def _run_command(self, target: ActionOutcomeTargetConfig) -> CheckResult:
        started = perf_counter()
        try:
            completed = subprocess.run(
                target.command,
                shell=True,
                cwd=_working_dir(self.project_root, target.working_dir),
                env=_env(target.env, self.dev_server_url),
                capture_output=True,
                text=True,
                timeout=target.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name=f"{self.label}: {target.name}",
                category=self.category,
                output=_join_output(str(exc.stdout or ""), str(exc.stderr or ""), f"timeout after {target.timeout}s"),
                elapsed_sec=elapsed,
                details={"command": target.command, "timeout": target.timeout, "actions": _action_matrix(target)},
            )

        elapsed = perf_counter() - started
        return CheckResult(
            passed=completed.returncode == 0,
            name=f"{self.label}: {target.name}",
            category=self.category,
            output=_join_output(completed.stdout, completed.stderr, f"exit_code={completed.returncode}"),
            elapsed_sec=elapsed,
            details={"command": target.command, "exit_code": completed.returncode, "actions": _action_matrix(target)},
        )

    def _run_http_flow(self, client: httpx.Client, target: ActionOutcomeTargetConfig) -> CheckResult:
        started = perf_counter()
        invoke = target.invoke
        observe = target.observe
        if invoke is None or observe is None:
            return _missing_config(self.category, self.missing_config_message)

        invoke_url = _resolve_url(invoke.url, self.dev_server_url)
        try:
            invoke_response = client.request(
                invoke.method,
                invoke_url,
                headers=invoke.headers or None,
                data=invoke.body if invoke.body is not None else None,
                json=invoke.json if invoke.json is not None else None,
                timeout=invoke.timeout,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name=f"{self.label}: {target.name}",
                category=self.category,
                output=f"invoke request failed for {invoke_url}: {exc}",
                elapsed_sec=elapsed,
                details={"invoke_url": invoke_url, "actions": _action_matrix(target)},
            )

        if invoke_response.status_code != invoke.expected_status:
            elapsed = perf_counter() - started
            return CheckResult(
                passed=False,
                name=f"{self.label}: {target.name}",
                category=self.category,
                output=f"{invoke.method} {invoke_url} -> HTTP {invoke_response.status_code}, expected {invoke.expected_status}",
                elapsed_sec=elapsed,
                details={
                    "invoke_url": invoke_url,
                    "status_code": invoke_response.status_code,
                    "actions": _action_matrix(target),
                },
            )

        observe_url = _resolve_url(observe.url, self.dev_server_url)
        attempts = 0
        last_output = ""
        deadline = perf_counter() + target.max_wait_seconds
        while True:
            attempts += 1
            try:
                response = client.request(
                    observe.method,
                    observe_url,
                    headers=observe.headers or None,
                    data=observe.body if observe.body is not None else None,
                    json=observe.json if observe.json is not None else None,
                    timeout=observe.timeout,
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last_output = f"observe request failed for {observe_url}: {exc}"
            else:
                body = str(getattr(response, "text", "") or "")
                status_ok = response.status_code == observe.expected_status
                expect_ok = target.expect_text is None or target.expect_text in body
                forbid_ok = target.forbid_text is None or target.forbid_text not in body
                last_output = (
                    f"{observe.method} {observe_url} -> HTTP {response.status_code}, "
                    f"expected {observe.expected_status}, expect_text={target.expect_text!r}, "
                    f"forbid_text={target.forbid_text!r}, attempt={attempts}"
                )
                if status_ok and expect_ok and forbid_ok:
                    elapsed = perf_counter() - started
                    return CheckResult(
                        passed=True,
                        name=f"{self.label}: {target.name}",
                        category=self.category,
                        output=f"invoke OK; outcome observed after {attempts} attempt(s): {last_output}",
                        elapsed_sec=elapsed,
                        details={
                            "invoke_url": invoke_url,
                            "observe_url": observe_url,
                            "attempts": attempts,
                            "actions": _action_matrix(target),
                        },
                    )

            if perf_counter() >= deadline:
                elapsed = perf_counter() - started
                return CheckResult(
                    passed=False,
                    name=f"{self.label}: {target.name}",
                    category=self.category,
                    output=f"invoke OK; outcome not observed within {target.max_wait_seconds:.3f}s: {last_output}",
                    elapsed_sec=elapsed,
                    details={
                        "invoke_url": invoke_url,
                        "observe_url": observe_url,
                        "attempts": attempts,
                        "actions": _action_matrix(target),
                    },
                )
            time.sleep(max(0.0, min(target.poll_interval, deadline - perf_counter())))


def skipped_result(category: str, name: str, reason: str) -> CheckResult:
    return CheckResult(
        passed=False,
        name=name,
        category=category,
        skipped=True,
        output=f"skipped: {reason}",
        elapsed_sec=0.0,
    )


def _missing_config(category: str, message: str) -> CheckResult:
    names = {
        "db": "DB up",
        "dev-server": "Dev server up",
        "connectivity": "Smoke connectivity",
        "e2e": "Real-browser E2E",
        "crud-flow": "CRUD flow",
        "action-outcome": "Action outcome",
        "global-action": "Global action",
    }
    return CheckResult(
        passed=False,
        name=names.get(category, category),
        category=category,
        output=message,
        elapsed_sec=0.0,
    )


def _join_output(stdout: str, stderr: str, footer: str) -> str:
    parts = []
    if stdout:
        parts.append(f"stdout:\n{stdout.rstrip()}")
    if stderr:
        parts.append(f"stderr:\n{stderr.rstrip()}")
    parts.append(footer)
    return "\n".join(parts)


def _resolve_url(url: str, base_url: str | None) -> str:
    rendered = _render_token(url, base_url)
    if rendered.startswith(("http://", "https://")):
        return rendered
    if not base_url:
        return rendered
    return urljoin(base_url.rstrip("/") + "/", rendered.lstrip("/"))


def _render_token(value: str, dev_server_url: str | None) -> str:
    return value.replace("{{dev_server_url}}", dev_server_url or "")


def _working_dir(project_root: Path, working_dir: str | None) -> Path:
    if not working_dir:
        return project_root
    path = Path(working_dir).expanduser()
    if path.is_absolute():
        return path
    return project_root / path


def _env(env_config: dict[str, str], dev_server_url: str | None) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in env_config.items():
        env[key] = _render_token(value, dev_server_url)
    return env


def _response_elapsed_seconds(response: httpx.Response) -> float | None:
    elapsed = getattr(response, "elapsed", None)
    if elapsed is None:
        return None
    total_seconds = getattr(elapsed, "total_seconds", None)
    if callable(total_seconds):
        return float(total_seconds())
    return None


def _header_assertions(
    response: httpx.Response,
    expected: dict[str, str],
    *,
    should_contain: bool,
) -> tuple[bool, str]:
    if not expected:
        return True, ""

    failures: list[str] = []
    fragments: list[str] = []
    for name, needle in expected.items():
        value = _response_header(response, name)
        contains = needle in value
        ok = contains if should_contain else not contains
        relation = "contains" if should_contain else "does not contain"
        fragments.append(f"{name} {relation} {needle!r}: {ok}")
        if not ok:
            failures.append(name)
    label = "expect_headers" if should_contain else "forbid_headers"
    return not failures, f", {label}=[{'; '.join(fragments)}]"


def _response_header(response: httpx.Response, name: str) -> str:
    headers = getattr(response, "headers", {}) or {}
    getter = getattr(headers, "get", None)
    if callable(getter):
        direct = getter(name)
        if direct is not None:
            return str(direct)
        lower = getter(name.lower())
        if lower is not None:
            return str(lower)
    for key, value in getattr(headers, "items", lambda: [])():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def _action_matrix(target: ActionOutcomeTargetConfig) -> list[dict[str, Any]]:
    return [
        {
            "id": action.id,
            "verb": action.verb or "",
            "target": action.target or "",
            "trigger": action.trigger or "",
            "outcomes": [outcome.name for outcome in action.outcomes if outcome.required],
            "actor": action.actor or "",
            "actors": action.actors,
        }
        for action in target.actions
    ]
