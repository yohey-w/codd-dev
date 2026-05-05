"""curl verification template."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from codd.deployment.providers import (
    VerificationResult,
    VerificationTemplate,
    register_verification_template,
)


DEFAULTS_PATH = Path(__file__).parents[2] / "defaults" / "verification_templates.yaml"


def _load_timeout_seconds() -> float:
    try:
        defaults = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8")) or {}
    except OSError:
        return 30.0
    timeout = defaults.get("templates", {}).get("curl", {}).get("timeout", 30)
    try:
        return float(timeout)
    except (TypeError, ValueError):
        return 30.0


def _target(runtime_state: Any) -> str:
    return str(getattr(runtime_state, "target", "") or "")


def _health_url(target: str) -> str:
    if target.startswith(("http://", "https://")):
        parsed = urlsplit(target)
        return urlunsplit((parsed.scheme, parsed.netloc, "/api/health", "", ""))
    return "/api/health"


@register_verification_template("curl")
class CurlTemplate(VerificationTemplate):
    """Generate and execute curl verification commands."""

    def __init__(self, timeout: float | None = None, dry_run: bool = False) -> None:
        self.timeout = timeout if timeout is not None else _load_timeout_seconds()
        self.dry_run = dry_run

    def generate_test_command(self, runtime_state, test_kind: str) -> str:
        actual_check_command = getattr(runtime_state, "actual_check_command", None)
        if actual_check_command:
            return str(actual_check_command)

        kind = test_kind.lower()
        target = _target(runtime_state)
        if kind == "health":
            url = _health_url(target)
            return f"curl -s -o /dev/null -w '%{{http_code}}' {url}"
        if kind == "smoke":
            return f"curl -s -o /dev/null -w '%{{http_code}}' -X POST {target}"
        return f"curl -s -o /dev/null -w '%{{http_code}}' {target}"

    def execute(self, command: str) -> VerificationResult:
        if self.dry_run:
            return VerificationResult(passed=True, output=command, duration=0.0)

        started_at = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started_at
            output = exc.stderr or exc.stdout or f"Timed out after {self.timeout:g}s"
            return VerificationResult(passed=False, output=str(output), duration=duration)

        duration = time.monotonic() - started_at
        output = completed.stdout or completed.stderr
        return VerificationResult(
            passed=completed.returncode == 0 and "200" in completed.stdout,
            output=output,
            duration=duration,
        )
