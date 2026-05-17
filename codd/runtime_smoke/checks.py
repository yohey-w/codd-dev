"""Runtime smoke check implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urljoin
import subprocess

import httpx

from codd.runtime_smoke.config import ConnectivityConfig, DbCheckConfig, DevServerConfig, E2eConfig


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
        passed = response.status_code == check.expected_status and elapsed_for_gate <= check.timeout
        if check.save_cookie_jar:
            cookie_jars[check.save_cookie_jar] = httpx.Cookies(client.cookies)

        return CheckResult(
            passed=passed,
            name=f"Smoke connectivity: {check.name}",
            category="connectivity",
            output=(
                f"{check.method} {url} -> HTTP {response.status_code}, "
                f"expected {check.expected_status}, elapsed {elapsed_for_gate:.3f}s <= {check.timeout:.3f}s"
            ),
            elapsed_sec=elapsed,
            details={
                "index": index,
                "method": check.method,
                "url": url,
                "status_code": response.status_code,
                "response_elapsed_sec": elapsed_for_gate,
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


def _response_elapsed_seconds(response: httpx.Response) -> float | None:
    elapsed = getattr(response, "elapsed", None)
    if elapsed is None:
        return None
    total_seconds = getattr(elapsed, "total_seconds", None)
    if callable(total_seconds):
        return float(total_seconds())
    return None
