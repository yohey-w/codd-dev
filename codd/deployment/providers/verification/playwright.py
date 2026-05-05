"""Playwright verification template."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

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
        return 60.0
    timeout = defaults.get("templates", {}).get("playwright", {}).get("timeout", 60000)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return 60.0
    return timeout_value / 1000 if timeout_value > 1000 else timeout_value


def _project_root(runtime_state: Any) -> Path:
    root = getattr(runtime_state, "project_root", None) or getattr(runtime_state, "root", None)
    return Path(root) if root else Path.cwd()


@register_verification_template("playwright")
class PlaywrightTemplate(VerificationTemplate):
    """Generate and execute Playwright verification commands."""

    def __init__(self, timeout: float | None = None) -> None:
        self.timeout = timeout if timeout is not None else _load_timeout_seconds()

    def generate_test_command(self, runtime_state, test_kind: str) -> str:
        kind = test_kind.lower()
        test_dir = "tests/e2e/" if kind == "e2e" else "tests/smoke/"
        parts = ["npx", "playwright", "test", test_dir, "--reporter=line"]

        config_path = _project_root(runtime_state) / "playwright.config.ts"
        if config_path.exists():
            parts.extend(["--config", str(config_path)])

        target = str(getattr(runtime_state, "target", "") or "")
        if kind == "smoke" and "login" in target.lower():
            parts.extend(["--grep", "login"])

        return " ".join(parts)

    def execute(self, command: str) -> VerificationResult:
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
        if completed.returncode == 0:
            return VerificationResult(passed=True, output=completed.stdout, duration=duration)
        return VerificationResult(
            passed=False,
            output=completed.stderr or completed.stdout,
            duration=duration,
        )

    def find_spec_files(self, project_root: Path, test_kind: str) -> list[Path]:
        if test_kind.lower() == "e2e":
            pattern = "tests/e2e/**/*.spec.ts"
        else:
            pattern = "tests/smoke/**/*.test.ts"
        return sorted(project_root.glob(pattern))
