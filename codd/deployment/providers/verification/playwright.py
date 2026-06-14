"""Playwright verification template."""

from __future__ import annotations

import subprocess
import shlex
import time
from collections.abc import Mapping
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

    def __init__(self, timeout: float | None = None, config: Mapping[str, Any] | None = None) -> None:
        config_map = dict(config) if isinstance(config, Mapping) else {}
        configured_timeout = _positive_float(config_map.get("timeout"))
        self.timeout = timeout if timeout is not None else configured_timeout or _load_timeout_seconds()
        self.project = _optional_string(config_map.get("project"))

    def generate_test_command(self, runtime_state, test_kind: str) -> str:
        kind = test_kind.lower()
        project_root = _project_root(runtime_state)
        test_target = _specific_test_target(runtime_state, kind, project_root)
        specific_target = test_target is not None
        if test_target is None:
            test_target = "tests/e2e/" if kind == "e2e" else "tests/smoke/"
        parts = ["npx", "playwright", "test", shlex.quote(test_target), "--reporter=line"]

        config_path = project_root / "playwright.config.ts"
        if config_path.exists():
            parts.extend(["--config", shlex.quote(str(config_path))])
        if self.project:
            parts.extend(["--project", shlex.quote(self.project)])

        target = str(getattr(runtime_state, "target", "") or "")
        if not specific_target and kind == "smoke" and "login" in target.lower():
            parts.extend(["--grep", "login"])

        return " ".join(parts)

    def execute(self, command: str, cwd: Path | None = None) -> VerificationResult:
        started_at = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
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


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _specific_test_target(runtime_state: Any, test_kind: str, project_root: Path) -> str | None:
    source = getattr(runtime_state, "source", None)
    if not source:
        return None
    source_text = str(source).strip()
    if not source_text:
        return None
    expected_prefix = "tests/e2e/" if test_kind == "e2e" else "tests/smoke/"
    normalized = source_text.replace("\\", "/")
    if not normalized.startswith(expected_prefix):
        return None

    path = Path(source_text)
    candidate = path if path.is_absolute() else project_root / path
    if not candidate.is_file():
        return None
    try:
        return candidate.relative_to(project_root).as_posix()
    except ValueError:
        return str(candidate)
