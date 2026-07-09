"""Contract-driven verify EXECUTOR (Contract Kernel Step 4 → wired at Step 6).

Runs a :class:`~codd.languages.verify_plan.VerifyRunPlan`, captures + parses its
machine-readable report, and classifies the outcome into a
:class:`~codd.languages.verify_plan.VerifyClass`. This is the imperative half that
:mod:`codd.languages.verify_plan`'s pure classifier deliberately is NOT: it spawns
the process, persists/locates the report, runs the report adapter, and then maps
the observed signals to a class.

WIRED IN. ``codd.repair.verify_runner.VerifyRunner`` routes any project with a
resolvable language contract + verify command + available report adapter through
this executor (Step 6: "run the contract executor; its verdict is FINAL, no legacy
rescue"); only a declared-language-less project, an explicit author test-command
override, a structural-only opt-out, or a missing report adapter on a
``legacy_compatible`` profile still falls back to the legacy ladder. Exercised both
by fixtures (``tests/languages/test_verify_executor.py``) and, live, by every
project whose language profile resolves.

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
* a required test set (``plan.required_test_sets``) had ZERO executed files in the
  parsed report → ``SCOPE_MISSING`` (Step 5; the "unit-only PASS" hole). Checked
  LAST, after the zero/failed/rc!=0 branches (so a more fundamental failure always
  wins) and just before PASS (so a scope miss on an exit-0 clean report is RED).
* else (rc==0, report parsed, collected ≥ floor, no failed/skipped, every required
  scope covered) → ``PASS``.

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
import shutil
import subprocess  # noqa: S404 — argv is from the trusted language profile, shell=False
from dataclasses import dataclass, replace
from pathlib import Path

from .builtin_adapters import ensure_builtin_adapters_registered
from .registry import AdapterRegistry, default_adapter_registry
from .verify_plan import VerifyClass, VerifyRunPlan
from .adapters.runner_report import RunnerExecution

#: Default wall-clock cap for a verify subprocess. A run that exceeds it is a FAIL
#: (a timeout is never green), never a hang that blocks the gate forever.
DEFAULT_VERIFY_TIMEOUT_SECONDS = 1800

#: Any of these surviving in the plan's cwd/argv/env/report_path AFTER
#: :func:`codd.languages.verify_plan.build_verify_plan` means the resolved language
#: layout could not supply a concrete value for it (e.g. ``{test_root}`` is
#: ambiguous when a profile declares zero or multiple test sets) — the executor
#: must NOT spawn in a literal ``{module_root}`` dir / with a literal ``{test_root}``
#: argument / write to a literal ``{report}`` path. Mirrors
#: :data:`codd.stack.command_plan._KNOWN_LAYOUT_PLACEHOLDERS` (the v2.75 cwd-bug
#: class), applied here to the core language verify plan.
_KNOWN_VERIFY_PLACEHOLDERS = (
    "{module_root}",
    "{repo_root}",
    "{manifest_root}",
    "{test_root}",
    "{report}",
)


def _unresolved_verify_placeholders(plan: VerifyRunPlan) -> list[str]:
    """Human labels for any ``{placeholder}`` the plan-build substitution did not resolve.

    Empty ⇒ every cwd/argv/env/report_path value is concrete (safe to spawn). Mirrors
    :func:`codd.stack.command_plan._unsubstituted_placeholders`.
    """
    problems: list[str] = []
    if any(tok in (plan.cwd or "") for tok in _KNOWN_VERIFY_PLACEHOLDERS):
        problems.append(f"cwd={plan.cwd!r}")
    for arg in plan.argv:
        if any(tok in (arg or "") for tok in _KNOWN_VERIFY_PLACEHOLDERS):
            problems.append(f"argv:{arg!r}")
    for key, value in plan.env.items():
        if any(tok in str(value) for tok in _KNOWN_VERIFY_PLACEHOLDERS):
            problems.append(f"env[{key}]={value!r}")
    if plan.report_path and any(tok in plan.report_path for tok in _KNOWN_VERIFY_PLACEHOLDERS):
        problems.append(f"report_path={plan.report_path!r}")
    return problems


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
    #: F7b — the executor's captured stdout / stderr from the run (empty when the
    #: command never spawned). The PRIMARY greenfield contract path parses only a
    #: machine-readable report (:class:`RunnerExecution`), which carries file-level
    #: signals but NO assertion text; carrying the raw output here lets the repair
    #: runner window it into the failure evidence so the RCA/propose prompt sees the
    #: actual failing assertion (ending the "skipped/todo" / "fixture I/O"
    #: hallucinations). Additive; defaults keep every existing constructor valid.
    stdout: str = ""
    stderr: str = ""

    @property
    def is_green(self) -> bool:
        return self.verify_class.is_green


def execute_verify_plan(
    plan: VerifyRunPlan,
    project_root: Path,
    *,
    adapter_registry: AdapterRegistry | None = None,
    timeout: float | None = None,
    exec_path_prepend: tuple[str, ...] = (),
) -> VerifyExecutionResult:
    """Run ``plan`` under ``project_root`` and classify the outcome (anti-false-green).

    ``exec_path_prepend`` is a caller-supplied list of directories to prepend to the
    spawn's ``PATH`` (highest precedence first). It is a language-AGNOSTIC seam: the
    executor knows nothing of what lives in those dirs — it only prepends the ones
    that ACTUALLY EXIST as absolute directories, so an UNCHANGED bare ``argv[0]``
    resolves to a binary the caller materialized (the caller — the verify runner —
    reads the dirs from a harness-owned state artifact). The DEFAULT ``()`` prepends
    nothing, so the spawn env is byte-identical to today for every existing caller.

    Steps, in order:

    a. cwd = ``project_root / plan.cwd`` (or ``project_root``); env = ``os.environ``
       copy updated with ``plan.env``, then any existing ``exec_path_prepend`` dirs
       prepended to ``PATH``.
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

    # (a2) Unsubstituted-placeholder guard (the v2.75 cwd-bug class, applied to the
    # core verify plan): build_verify_plan() substitutes {module_root}/{repo_root}/
    # {manifest_root}/{test_root}/{report} at plan-build time; a token the resolved
    # layout could not supply is left literal (e.g. {test_root} is ambiguous when a
    # profile declares zero or multiple test sets). Refuse to spawn with a literal
    # "{...}" cwd/argv/env/report_path — never a silent pass, never a run against a
    # wrong or partial path. Mirrors codd.stack.command_plan's own guard for the
    # sibling stack-command subsystem.
    unresolved = _unresolved_verify_placeholders(plan)
    if unresolved:
        return VerifyExecutionResult(
            verify_class=VerifyClass.CONFIG_ERROR,
            returncode=None,
            execution=None,
            detail=(
                f"verify plan has unsubstituted layout placeholder(s) {unresolved}; "
                "refusing to spawn in an unresolved path (a literal '{...}' cwd/argv/"
                "env/report_path is the v2.75 cwd-bug class — RED, not a benign miss). "
                "The resolved language layout did not provide a value for it."
            ),
        )

    # (b) Anti-false-green: remove any STALE report before the run so a leftover
    # green report can never be mistaken for this run's output. The report SHAPE
    # is runner-defined, not language-defined: most runners write a single file
    # (vitest JSON, a `.trx`), but Maven Surefire writes a whole DIRECTORY of
    # per-class report files (`target/surefire-reports/`) — see
    # SurefireXmlReportAdapter, which accepts either shape. A bare ``unlink()``
    # only removes files: called on an existing, populated report DIRECTORY it
    # raises ``IsADirectoryError`` (an ``OSError`` subclass), which used to be
    # misreported as "could not remove" even though the real fix is just "use
    # the directory-aware removal" — so branch on the actual on-disk shape
    # (mirrors the same dir-vs-file removal already used by
    # ``coverage_execution_coherence.py`` for the identical stale-report concern).
    report_path: Path | None = None
    if plan.report_path:
        report_path = (project_root / plan.report_path)
        if not report_path.is_absolute():
            report_path = report_path.resolve()
        try:
            if report_path.is_dir():
                shutil.rmtree(report_path)
            else:
                report_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # An unremovable stale report (file OR directory) is itself an
            # observability hazard: we cannot guarantee what we read next is THIS
            # run's report → fail-closed.
            return VerifyExecutionResult(
                verify_class=VerifyClass.REPORT_UNREADABLE,
                returncode=None,
                execution=None,
                detail=f"could not remove stale report at {report_path} before run",
            )

    # (a3) Prepend caller-supplied directories to PATH (existence-checked, absolute
    # only). This is where an unchanged bare ``argv[0]`` gets resolved to a binary
    # the caller materialized. A non-existent / non-absolute entry (a forged or
    # stale state artifact) is DROPPED, so a bogus dir can never silently redirect
    # the spawn — it simply falls through to normal PATH resolution (TOOL_MISSING if
    # the tool is genuinely absent). No entries ⇒ PATH is untouched (byte-identical).
    prepend = [d for d in exec_path_prepend if d and os.path.isabs(d) and os.path.isdir(d)]
    if prepend:
        env["PATH"] = os.pathsep.join([*prepend, env.get("PATH", "")])

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
    result = _classify(plan, returncode, report_path, adapter, project_root)
    # F7b: carry the captured output on the result so a not-green contract failure
    # can surface the actual assertion text into repair evidence (the parsed report
    # has none). Green results carry it harmlessly; the repair runner only reads it
    # on a failure. Only reachable when the command actually spawned (``completed``).
    return replace(result, stdout=completed.stdout or "", stderr=completed.stderr or "")


def _norm_rel_posix(path: str) -> str:
    """Normalize a path to comparable project-relative POSIX form for scope matching.

    Mirrors how :class:`RunnerExecution` already stores executed files (project-
    relative, POSIX) but is defensive about a stray ``./`` prefix / backslash and a
    trailing slash so a root and an executed-file path compare on the same footing.
    Uses PREFIX stripping (not ``lstrip("./")``, which is a character class and would
    wrongly eat a legitimate leading dot, e.g. ``.codd/x`` → ``codd/x``).
    """
    p = str(path or "").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def _set_is_covered(root: str, executed: list[str]) -> bool:
    """True iff some executed file falls under ``root`` (a directory prefix match).

    ``root`` is normalized like the executed paths. A root of ``.`` or ``""`` (a
    colocated set, whose tests sit anywhere among the sources) is trivially covered
    by ANY executed file. A specific root such as ``tests/e2e`` is covered only by an
    executed file equal to it or beneath it (``path == root`` or
    ``path.startswith(root + "/")``) — so ``tests/unit/...`` never counts as covering
    ``tests/e2e``.
    """
    norm_root = _norm_rel_posix(root)
    if norm_root in ("", "."):
        return bool(executed)
    prefix = norm_root + "/"
    return any(p == norm_root or p.startswith(prefix) for p in executed)


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

    # Step 5 — SCOPE observation. A clean, exit-0, parsed report can still be a
    # FALSE green if it never ran a test set it was REQUIRED to cover (the
    # "unit-only PASS" hole: a mutation that only ran unit tests while an e2e set
    # was required). For each required ``(set_id, root)``, check whether ANY file
    # the report says it executed falls under that set's root; a required set with
    # ZERO covering executed files → SCOPE_MISSING (RED).
    #
    # Ordering: this runs AFTER the zero/failed/rc!=0 branches above (so a more
    # fundamental failure always wins — a report with a FAILED file is FAIL even if
    # e2e is also uncovered, because the failed-file branch returned earlier) and
    # is the LAST not-green gate BEFORE PASS (so a scope miss on an otherwise
    # green-looking exit-0 report is correctly RED, never PASS).
    #
    # We only have something to check when a report was parsed; the no-report /
    # failing cases are already classified above. ``execution.executed_files`` are
    # project-relative, POSIX, normalized test-file paths (see RunnerExecution), so
    # we match a root the same way: strip leading ``./``, POSIX separators, and
    # treat ``root`` as a directory PREFIX — a path covers the root iff
    # ``path == root`` or ``path.startswith(root + "/")``. A root of ``.`` or ``""``
    # (a colocated set) matches every executed file, so any executed file trivially
    # covers it.
    if execution is not None and plan.required_test_sets:
        executed = [_norm_rel_posix(p) for p in execution.executed_files]
        for set_id, raw_root in plan.required_test_sets:
            if not _set_is_covered(raw_root, executed):
                return VerifyExecutionResult(
                    verify_class=VerifyClass.SCOPE_MISSING,
                    returncode=returncode,
                    execution=execution,
                    detail=(
                        f"required test set {set_id!r} (root {raw_root!r}) had zero "
                        f"executed files in the report — the verify did not run what it "
                        f"must cover (a report that is green only because it skipped a "
                        f"required set is not an authentic pass)"
                    ),
                )

    # All not-green checks passed: rc==0, required report parsed, collected ≥ floor,
    # no failed/skipped, every required scope covered → the only green class.
    return VerifyExecutionResult(
        verify_class=VerifyClass.PASS,
        returncode=returncode,
        execution=execution,
        detail="verify passed (exit 0, report parsed, collected ≥ minimum, no failed/skipped tests)",
    )
