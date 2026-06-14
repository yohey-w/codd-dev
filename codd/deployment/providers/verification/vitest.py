"""vitest CLI end-to-end verification template.

The Playwright template (``playwright.py``) drives a BROWSER e2e: it runs
``npx playwright test`` against ``*.spec.ts`` files. That is the wrong runner
for a **CLI** project — a CLI converter / tool has vitest (or jest) end-to-end
tests that exercise the compiled binary's stdin/stdout/exit-code surface, and
running those under Playwright yields "No tests found" (the harness, not the
code, is broken). Routing in the extractor (:func:`_verification_template_ref`)
now sends ``e2e_modality == "cli"`` TypeScript/JavaScript e2e nodes here.

This template runs a single e2e file (or the e2e directory) under
``npx vitest run`` and applies the same ANTI-FALSE-GREEN executed-count
discipline the rest of the verify layer enforces: a run that COLLECTS/RUNS ZERO
tests is a HARD FAIL even on exit 0 (``vitest run`` exits 0 with "No test files
found" — that must never count as green-on-nothing).
"""

from __future__ import annotations

import re
import shlex
import subprocess
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

#: vitest's "nothing ran" signatures. ``vitest run`` exits 0 when it finds no
#: test files, so the exit code alone cannot distinguish green from green-on-
#: nothing — we additionally scan the output.
_NO_TESTS_MARKERS: tuple[str, ...] = (
    "no test files found",
    "no tests found",
    "no test suites found",
)
#: ``Tests  <N> passed`` / ``Test Files  <N> passed`` summary — a positive
#: collected-count signal. If neither a positive count nor a failure marker is
#: present, the run is treated as "collected nothing" (honest hard fail).
_TESTS_RAN_RE = re.compile(r"tests?\s+\d+\s+(?:passed|failed)", re.IGNORECASE)
_TEST_FILES_RE = re.compile(r"test files?\s+\d+\s+(?:passed|failed)", re.IGNORECASE)


def _load_timeout_seconds() -> float:
    try:
        defaults = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8")) or {}
    except OSError:
        return 60.0
    timeout = defaults.get("templates", {}).get("vitest", {}).get("timeout", 60000)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return 60.0
    return timeout_value / 1000 if timeout_value > 1000 else timeout_value


def _project_root(runtime_state: Any) -> Path:
    root = getattr(runtime_state, "project_root", None) or getattr(runtime_state, "root", None)
    return Path(root) if root else Path.cwd()


@register_verification_template("vitest")
class VitestTemplate(VerificationTemplate):
    """Generate and execute ``npx vitest run`` commands for CLI e2e tests."""

    def __init__(self, timeout: float | None = None, config: Mapping[str, Any] | None = None) -> None:
        config_map = dict(config) if isinstance(config, Mapping) else {}
        configured_timeout = _positive_float(config_map.get("timeout"))
        self.timeout = timeout if timeout is not None else configured_timeout or _load_timeout_seconds()

    def generate_test_command(self, runtime_state, test_kind: str) -> str:
        kind = test_kind.lower()
        project_root = _project_root(runtime_state)
        test_target = _specific_test_target(runtime_state, kind, project_root)
        if test_target is None:
            test_target = "tests/e2e/" if kind == "e2e" else "tests/smoke/"
        # ``vitest run`` = non-watch single run; the path positional scopes it to
        # the e2e file/dir so a CLI e2e node runs its OWN test, not the suite.
        return " ".join(["npx", "vitest", "run", shlex.quote(test_target)])

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
        combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        # ANTI-FALSE-GREEN: vitest exits 0 with "No test files found" — a
        # collected/ran count of ZERO is a HARD FAIL even on exit 0.
        if _collected_zero(combined):
            return VerificationResult(
                passed=False,
                output=(
                    "vitest collected/ran 0 tests (no test files found) — "
                    "treated as a hard failure (anti-false-green).\n" + combined
                ),
                duration=duration,
            )
        if completed.returncode == 0:
            return VerificationResult(passed=True, output=completed.stdout, duration=duration)
        return VerificationResult(
            passed=False,
            output=completed.stderr or completed.stdout,
            duration=duration,
        )

    def find_spec_files(self, project_root: Path, test_kind: str) -> list[Path]:
        # Generated CLI e2e tests are ``*.test.ts`` (vitest's default glob),
        # unlike Playwright's ``*.spec.ts``.
        if test_kind.lower() == "e2e":
            pattern = "tests/e2e/**/*.test.ts"
        else:
            pattern = "tests/smoke/**/*.test.ts"
        return sorted(project_root.glob(pattern))


def _collected_zero(output: str) -> bool:
    """True when the vitest output proves NO test was collected/run.

    Conservative: a "no test files" marker is an explicit zero; otherwise a run
    that shows neither a positive ``Tests N passed/failed`` summary NOR a
    ``Test Files N`` summary is also treated as zero (vitest always prints one
    of those when it ran anything).
    """
    lowered = output.lower()
    if any(marker in lowered for marker in _NO_TESTS_MARKERS):
        return True
    if _TESTS_RAN_RE.search(output) or _TEST_FILES_RE.search(output):
        return False
    return True


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
