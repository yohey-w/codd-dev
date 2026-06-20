"""Contract-driven verify EXECUTOR (Contract Kernel Step 4 — fixtures only).

Runs a :class:`~codd.languages.verify_plan.VerifyRunPlan`, captures + parses its
machine-readable report, and classifies the outcome into a
:class:`~codd.languages.verify_plan.VerifyClass`. This is the imperative half that
:mod:`codd.languages.verify_plan`'s pure classifier deliberately is NOT: it spawns
the process, persists/locates the report, runs the report adapter, and then maps
the observed signals to a class.

NOT WIRED IN. The live ``VerifyRunner`` still uses the legacy path; this executor
is exercised only by fixtures (``tests/languages/test_verify_executor.py``). The
switch is Step 6.

THE CARDINAL RULE (anti-false-green). A not-green signal ALWAYS beats an
exit-0 / clean-looking result. Every observation failure — a missing report, an
unreadable report, zero collected tests, a failed test, a SKIPPED test (a skip is
not an authentic pass), a missing report adapter, a timeout — is classified as a
not-green class BEFORE any PASS is considered. There is deliberately NO "allow"
path: nothing in this module lets a zero/missing/failed/skipped observation become
PASS. The observation policy fields
(:class:`~codd.languages.profile.VerifyObservationPolicy`) are the invariant; they
only ever raise the bar (``min_collected_tests``), never lower it.

Mapping of :class:`~codd.languages.adapters.runner_report.RunnerExecution` fields
to classes (the anti-false-green ordering, GPT §5):

* ``report_required`` and report file absent → ``REPORT_MISSING``.
* report present but ``adapter.parse(...)`` raises → ``REPORT_UNREADABLE``.
* ``execution.total_cases == 0`` AND ``execution.executed_files == ∅`` (no executed
  file) → ``ZERO_TESTS``; also ``execution.total_cases < observation.min_collected_tests``
  → ``ZERO_TESTS`` (collected below the floor is "effectively no tests").
* ``execution.executed_failed_files`` non-empty (any failed test/file) → ``FAIL``.
* a SKIPPED test was observed (``observation.skipped_tests == "red"``) → ``FAIL``
  (skip is not an authentic pass; see below for how a skip is detected).
* ``returncode != 0`` → ``FAIL`` (a nonzero exit beats a green-looking report).
* else (rc==0, report parsed, collected ≥ floor, no failed/skipped) → ``PASS``.

How a SKIP is detected from :class:`RunnerExecution`. The adapters fold a skipped
case into the FILE-level signal: a file carrying ANY skipped (or failed) case is
put in ``executed_failed_files``, never ``executed_passed_files`` (see the vitest /
go-test adapters — "a skip proves nothing"). So a skipped test surfaces here as a
``executed_failed_files`` member, which the failed-test branch already reds. We
ALSO red the count-mismatch shape (``passed_cases < total_cases`` while no file is
marked failed) because a runner that reports cases but where passed < total has a
non-passed (failed/skipped) case the file rollup may not have attributed — a
defensive not-green so a partial report never sneaks to PASS.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 — argv is from the trusted language profile, shell=False
from dataclasses import dataclass
from pathlib import Path

from .builtin_adapters import ensure_builtin_adapters_registered
from .registry import AdapterRegistry, default_adapter_registry
from .verify_plan import VerifyClass, VerifyRunPlan
from .adapters.runner_report import RunnerExecution

#: Default wall-clock cap for a verify subprocess. A run that exceeds it is a FAIL
#: (a timeout is never green), never a hang that blocks the gate forever.
DEFAULT_VERIFY_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class VerifyExecutionResult:
    """Outcome of running a verify plan through the executor.

    ``verify_class`` is the anti-false-green verdict (only ``PASS`` is green).
    ``returncode`` is the subprocess exit code (``None`` if it never spawned / timed
    out). ``execution`` is the parsed :class:`RunnerExecution` (``None`` when no
    report was parsed — tool missing, report absent/unreadable, etc.). ``detail`` is
    a short human reason, always populated for a not-green class.
    """

    verify_class: VerifyClass
    returncode: int | None
    execution: RunnerExecution | None
    detail: str

    @property
    def is_green(self) -> bool:
        return self.verify_class.is_green


def execute_verify_plan(
    plan: VerifyRunPlan,
    project_root: Path,
    *,
    adapter_registry: AdapterRegistry | None = None,
    timeout: float | None = None,
) -> VerifyExecutionResult:
    """Run ``plan`` under ``project_root`` and classify the outcome (anti-false-green).

    Steps, in order:

    a. cwd = ``project_root / plan.cwd`` (or ``project_root``); env = ``os.environ``
       copy updated with ``plan.env``.
    b. If ``plan.report_path`` is set, UNLINK any stale report file BEFORE running —
       a leftover green report from a prior run must never be read as this run's
       result (the canonical stale-report false-green).
    c. ``subprocess.run(plan.argv, shell=False, …, capture_output=True, text=True,
       timeout=…)``. ``FileNotFoundError`` / ``OSError`` (tool not found / spawn
       failure) → ``TOOL_MISSING``; ``TimeoutExpired`` → ``FAIL`` (never green).
    d. If ``plan.report_capture == "stdout"``, persist the captured stdout to the
       report path (``mkdir`` parents) — ``go test -json`` writes no file itself.
    e. Resolve the runner_report adapter via
       :func:`ensure_builtin_adapters_registered` then ``registry.get(...)``. If the
       plan requires a report but the adapter is unavailable, the executor cannot
       observe → ``REPORT_UNREADABLE`` (fail-closed; never PASS).
    f. Classify with not-green BEFORE any PASS (see module docstring / GPT §5).
    """

    registry = adapter_registry if adapter_registry is not None else default_adapter_registry
    run_timeout = timeout if timeout is not None else DEFAULT_VERIFY_TIMEOUT_SECONDS

    # (a) Resolve cwd + env.
    cwd = (project_root / plan.cwd) if plan.cwd else project_root
    env = os.environ.copy()
    env.update(plan.env)

    # (b) Anti-false-green: unlink any STALE report before the run so a leftover
    # green report can never be mistaken for this run's output.
    report_path: Path | None = None
    if plan.report_path:
        report_path = (project_root / plan.report_path)
        if not report_path.is_absolute():
            report_path = report_path.resolve()
        try:
            report_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # An unremovable stale file is itself an observability hazard: we cannot
            # guarantee what we read next is THIS run's report → fail-closed.
            return VerifyExecutionResult(
                verify_class=VerifyClass.REPORT_UNREADABLE,
                returncode=None,
                execution=None,
                detail=f"could not remove stale report at {report_path} before run",
            )

    # (c) Run the command.
    try:
        completed = subprocess.run(  # noqa: S603 — trusted argv, shell=False
            list(plan.argv),
            shell=False,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=run_timeout,
        )
    except (FileNotFoundError, OSError) as exc:
        return VerifyExecutionResult(
            verify_class=VerifyClass.TOOL_MISSING,
            returncode=None,
            execution=None,
            detail=f"verify command could not spawn ({plan.command_str!r}): {exc}",
        )
    except subprocess.TimeoutExpired:
        return VerifyExecutionResult(
            verify_class=VerifyClass.FAIL,
            returncode=None,
            execution=None,
            detail=f"verify command timed out after {run_timeout}s ({plan.command_str!r})",
        )

    returncode = completed.returncode

    # (d) Persist captured stdout to the report path when the command streams its
    # machine-readable report to stdout (e.g. `go test -json`).
    if report_path is not None and (plan.report_capture or "").strip().lower() == "stdout":
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(completed.stdout or "", encoding="utf-8")
        except OSError as exc:
            return VerifyExecutionResult(
                verify_class=VerifyClass.REPORT_UNREADABLE,
                returncode=returncode,
                execution=None,
                detail=f"could not persist captured stdout to {report_path}: {exc}",
            )

    # (e) Resolve the runner_report adapter (fail-closed when a required report has
    # no adapter to read it — the executor cannot observe, so it must not PASS).
    ensure_builtin_adapters_registered(registry)
    adapter = None
    if plan.report_adapter is not None:
        adapter = registry.get("runner_report", plan.report_adapter)
    if plan.report_required and adapter is None:
        return VerifyExecutionResult(
            verify_class=VerifyClass.REPORT_UNREADABLE,
            returncode=returncode,
            execution=None,
            detail=(
                f"required runner_report adapter "
                f"{plan.report_adapter!r} is not registered — the executor cannot "
                "observe the report, so the run cannot be classified green "
                "(fail-closed; never PASS)"
            ),
        )

    # (f) Classify — not-green BEFORE any PASS (GPT §5 ordering).
    return _classify(plan, returncode, report_path, adapter, project_root)


def _classify(
    plan: VerifyRunPlan,
    returncode: int,
    report_path: Path | None,
    adapter: object | None,
    project_root: Path,
) -> VerifyExecutionResult:
    """Map observed signals to a class, not-green ordered before PASS (pure of I/O
    spawning — it only reads the report and inspects ``returncode``)."""

    # report_required and report file absent → REPORT_MISSING.
    if plan.report_required:
        if report_path is None or not report_path.exists():
            return VerifyExecutionResult(
                verify_class=VerifyClass.REPORT_MISSING,
                returncode=returncode,
                execution=None,
                detail=(
                    f"required report not found at {report_path} after the verify run "
                    "(an absent report is a not-green observation, never an empty pass)"
                ),
            )

    execution: RunnerExecution | None = None
    if plan.report_required and adapter is not None and report_path is not None:
        # report present but adapter.parse(...) raises → REPORT_UNREADABLE.
        try:
            execution = adapter.parse(report_path, project_root=project_root)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — ANY parse failure is unreadable, not a pass
            return VerifyExecutionResult(
                verify_class=VerifyClass.REPORT_UNREADABLE,
                returncode=returncode,
                execution=None,
                detail=(
                    f"report at {report_path} present but unparseable by adapter "
                    f"{plan.report_adapter!r}: {exc}"
                ),
            )

    if execution is not None:
        collected = execution.total_cases
        executed_files = execution.executed_files
        # zero tests: nothing collected AND no executed file → ZERO_TESTS.
        if collected == 0 and not executed_files:
            return VerifyExecutionResult(
                verify_class=VerifyClass.ZERO_TESTS,
                returncode=returncode,
                execution=execution,
                detail="report parsed but it observed zero tests (no cases, no executed files)",
            )
        # collected below the policy floor → ZERO_TESTS (effectively no tests). The
        # policy may RAISE min_collected_tests (stricter), never lower it below 1.
        if collected < plan.observation.min_collected_tests:
            return VerifyExecutionResult(
                verify_class=VerifyClass.ZERO_TESTS,
                returncode=returncode,
                execution=execution,
                detail=(
                    f"collected {collected} test(s), below the required minimum "
                    f"{plan.observation.min_collected_tests} (an under-collected run is "
                    "not an authentic pass)"
                ),
            )
        # any failed test/file → FAIL. A SKIPPED case is folded by the adapters into
        # executed_failed_files (a skip is not a clean pass), so this branch also
        # reds a skip-tainted file (observation.skipped_tests == "red").
        if execution.executed_failed_files:
            return VerifyExecutionResult(
                verify_class=VerifyClass.FAIL,
                returncode=returncode,
                execution=execution,
                detail=(
                    f"report shows failed/skipped test file(s): "
                    f"{sorted(execution.executed_failed_files)} (a fail or a skip is "
                    "not an authentic pass)"
                ),
            )
        # Defensive: per-case granularity available but passed < total ⇒ a non-passed
        # (failed/skipped) case exists that the file rollup did not attribute → red,
        # so a partial report never reaches PASS.
        if execution.test_level_available and execution.passed_cases < execution.total_cases:
            return VerifyExecutionResult(
                verify_class=VerifyClass.FAIL,
                returncode=returncode,
                execution=execution,
                detail=(
                    f"report shows {execution.passed_cases} passed of "
                    f"{execution.total_cases} collected — a non-passed (failed/skipped) "
                    "case is present, which is not an authentic pass"
                ),
            )

    # returncode != 0 → FAIL (a nonzero exit beats a green-looking report).
    if returncode != 0:
        return VerifyExecutionResult(
            verify_class=VerifyClass.FAIL,
            returncode=returncode,
            execution=execution,
            detail=f"verify command exited {returncode} (nonzero exit beats a green-looking report)",
        )

    # TODO(Step 5 — scope observation): once scope checking lands, also red a run
    # whose parsed execution does not cover every plan.must_include_test_sets (a
    # report where, e.g., only the unit set ran while an e2e set was required is a
    # not-green scope miss). Scope checking is deliberately NOT implemented here.

    # All not-green checks passed: rc==0, required report parsed, collected ≥ floor,
    # no failed/skipped → the only green class.
    return VerifyExecutionResult(
        verify_class=VerifyClass.PASS,
        returncode=returncode,
        execution=execution,
        detail="verify passed (exit 0, report parsed, collected ≥ minimum, no failed/skipped tests)",
    )
