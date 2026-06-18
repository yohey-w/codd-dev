"""pytest HTTP end-to-end verification template (Python-native web E2E).

The Playwright template (``playwright.py``) drives a BROWSER e2e against
``*.spec.ts`` files under a node toolchain. For a **Python** web app with an
HTTP surface and no explicit browser requirement, that is the wrong runner — the
project has no ``package.json`` and verify fails with ``Cannot find module
'@playwright/test'`` (the harness, not the code, is broken). The language-aware
harness resolver (:mod:`codd.e2e_harness`) routes such projects to a Python
``pytest`` HTTP e2e harness, and the extractor (:func:`_verification_template_ref`)
sends generated ``tests/e2e/test_*.py`` e2e nodes here.

This template runs a single Python e2e file (or the e2e directory) under
``python -m pytest -q`` and applies the same ANTI-FALSE-GREEN executed-count
discipline the rest of the verify layer enforces (mirroring ``vitest.py``): a run
that COLLECTS/RUNS ZERO tests is a HARD FAIL — pytest exits ``5`` and prints "no
tests ran" / "collected 0 items" when it finds nothing, which must never count as
green-on-nothing.
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

#: pytest's exit code when NO tests were collected (``EXIT_NOTESTSCOLLECTED``).
_PYTEST_NO_TESTS_EXIT_CODE = 5

#: pytest's "nothing ran" signatures. pytest exits 5 (not 0) when it collects no
#: tests, so the exit code alone normally suffices — but we additionally scan the
#: output so a wrapper/plugin that swallows exit 5 (or a ``-p no:cacheprovider``
#: style harness) still cannot pass on zero collected tests.
_NO_TESTS_MARKERS: tuple[str, ...] = (
    "no tests ran",
    "no tests collected",
    "collected 0 items",
    "collected 0 item",
)
#: ``collected <N> item(s)`` / ``<N> passed`` / ``<N> failed`` — a positive
#: collected/ran signal. ``collected 0 items`` is handled by the markers above
#: (this regex requires a non-zero leading digit).
_COLLECTED_RE = re.compile(r"collected\s+([1-9]\d*)\s+items?", re.IGNORECASE)
_RAN_RE = re.compile(r"\b\d+\s+(?:passed|failed|error|errors)\b", re.IGNORECASE)


def _load_timeout_seconds() -> float:
    try:
        defaults = yaml.safe_load(DEFAULTS_PATH.read_text(encoding="utf-8")) or {}
    except OSError:
        return 120.0
    timeout = defaults.get("templates", {}).get("pytest_http", {}).get("timeout", 120000)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return 120.0
    return timeout_value / 1000 if timeout_value > 1000 else timeout_value


def _project_root(runtime_state: Any) -> Path:
    root = getattr(runtime_state, "project_root", None) or getattr(runtime_state, "root", None)
    return Path(root) if root else Path.cwd()


@register_verification_template("pytest_http")
class PytestHttpTemplate(VerificationTemplate):
    """Generate and execute ``python -m pytest`` commands for Python HTTP e2e tests."""

    def __init__(self, timeout: float | None = None, config: Mapping[str, Any] | None = None) -> None:
        config_map = dict(config) if isinstance(config, Mapping) else {}
        configured_timeout = _positive_float(config_map.get("timeout"))
        self.timeout = timeout if timeout is not None else configured_timeout or _load_timeout_seconds()

    def generate_test_command(self, runtime_state, test_kind: str) -> str:
        actual_check_command = getattr(runtime_state, "actual_check_command", None)
        if actual_check_command:
            return str(actual_check_command)

        kind = test_kind.lower()
        project_root = _project_root(runtime_state)
        test_target = _specific_test_target(runtime_state, kind, project_root)
        if test_target is None:
            test_target = "tests/e2e/" if kind == "e2e" else "tests/smoke/"
        # ``python -m pytest`` (not bare ``pytest``) so the project's interpreter
        # /venv resolves the runner; the positional scopes the run to the e2e
        # file/dir so an e2e node runs its OWN test, not the whole suite.
        return f"python -m pytest -q {shlex.quote(test_target)}"

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
        combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        # ANTI-FALSE-GREEN: pytest exits 5 with "no tests ran" — a collected/ran
        # count of ZERO is a HARD FAIL regardless of exit code (even 0).
        if completed.returncode == _PYTEST_NO_TESTS_EXIT_CODE or _collected_zero(combined):
            return VerificationResult(
                passed=False,
                output=(
                    "pytest collected/ran 0 tests (no tests ran) — "
                    "treated as a hard failure (anti-false-green).\n" + combined
                ),
                duration=duration,
            )
        if completed.returncode == 0:
            # ANTI-FALSE-GREEN: exit 0 is necessary but NOT sufficient. Require a
            # POSITIVE execution signal ("collected N items" with N>=1, or an
            # "N passed/failed/error" summary) so a wrapper / actual_check_command
            # that swallows pytest's exit-5 and prints nothing cannot pass on zero
            # executed tests. A genuine ``pytest -q`` green run always prints the
            # "N passed" summary, so real runs are unaffected.
            if not _has_positive_execution(combined):
                return VerificationResult(
                    passed=False,
                    output=(
                        "pytest exited 0 but produced no positive execution evidence "
                        "(no 'collected N items' / 'N passed|failed|error' summary) — "
                        "cannot prove >=1 test ran; hard failure (anti-false-green).\n"
                        + combined
                    ),
                    duration=duration,
                )
            return VerificationResult(passed=True, output=completed.stdout, duration=duration)
        return VerificationResult(
            passed=False,
            output=completed.stderr or completed.stdout,
            duration=duration,
        )

    #: Python test-file shapes a pytest e2e run can collect: the pytest
    #: ``test_*.py`` / ``*_test.py`` conventions the generator emits under
    #: ``tests/e2e/``. Discovery in the extractor must see the same shapes.
    _RUNNABLE_PY_GLOBS: tuple[str, ...] = ("test_*.py", "*_test.py")

    def find_spec_files(self, project_root: Path, test_kind: str) -> list[Path]:
        base = "tests/e2e" if test_kind.lower() == "e2e" else "tests/smoke"
        found: dict[str, Path] = {}
        for leaf in self._RUNNABLE_PY_GLOBS:
            for path in project_root.glob(f"{base}/**/{leaf}"):
                found[str(path)] = path
        return sorted(found.values())


def _collected_zero(output: str) -> bool:
    """True when the pytest output proves NO test was collected/run.

    Conservative: an explicit "no tests ran" / "collected 0 items" marker is a
    definite zero. A run that shows a positive ``collected N items`` OR an
    ``N passed/failed/error`` summary is treated as non-zero. Absent any of
    those signals we do NOT force-fail here (the exit-code check in ``execute``
    already catches pytest's exit 5); this guard exists to defeat a wrapper that
    masks exit 5 while printing a "no tests ran" banner.
    """
    lowered = output.lower()
    if any(marker in lowered for marker in _NO_TESTS_MARKERS):
        return True
    return False


def _has_positive_execution(output: str) -> bool:
    """True when the pytest output PROVES >=1 test was collected or ran.

    A positive ``collected N items`` (N>=1) or an ``N passed/failed/error``
    summary is real evidence of execution. Required (in addition to exit 0)
    before a pytest_http run is credited green, so an exit-0 wrapper that emits
    no pytest summary cannot pass on nothing (anti-false-green).
    """
    return bool(_COLLECTED_RE.search(output) or _RAN_RE.search(output))


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
