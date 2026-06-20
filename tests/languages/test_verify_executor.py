"""Contract Kernel Step 4 — verify EXECUTOR anti-false-green matrix.

Exercises :func:`codd.languages.verify_executor.execute_verify_plan` across every
branch DETERMINISTICALLY, without depending on real go-test-json / vitest output:

* a FAKE ``RunnerReportAdapter`` (registered under ``("runner_report","fake")`` in a
  FRESH :class:`AdapterRegistry`) returns a crafted :class:`RunnerExecution`, so the
  classification of a parsed report is fully controlled; and
* tiny ``python -c`` fixture commands write (or do not write) a report file and exit
  with a chosen code, so spawn / exit / report-presence are controlled too.

THE INVARIANT UNDER TEST: a not-green signal ALWAYS beats an exit-0 / clean-looking
result. Each case asserts the EXACT :class:`VerifyClass`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from codd.languages.adapters.runner_report import (
    RunnerExecution,
    RunnerReportUnsupported,
)
from codd.languages.profile import VerifyObservationPolicy
from codd.languages.registry import AdapterRegistry
from codd.languages.verify_executor import execute_verify_plan
from codd.languages.verify_plan import VerifyClass, VerifyRunPlan


# ── fixtures: a controllable fake adapter + plan builder ───────────────────


@dataclass
class _FakeAdapter:
    """A runner_report adapter whose parse result is fully controlled by the test.

    ``execution`` is returned verbatim from :meth:`parse`; if ``raises`` is set,
    :meth:`parse` raises it instead (to exercise the REPORT_UNREADABLE branch).
    """

    execution: RunnerExecution | None = None
    raises: BaseException | None = None

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        if self.raises is not None:
            raise self.raises
        assert self.execution is not None
        return self.execution


def _registry_with(adapter: object | None) -> AdapterRegistry:
    """A fresh registry holding ``adapter`` at ``("runner_report","fake")`` (or empty)."""
    reg = AdapterRegistry()
    if adapter is not None:
        reg.register("runner_report", "fake", adapter)
    return reg


def _plan(
    *,
    argv: tuple[str, ...],
    report_path: str | None = "report.json",
    report_adapter: str | None = "fake",
    report_required: bool = True,
    report_capture: str | None = None,
    observation: VerifyObservationPolicy | None = None,
) -> VerifyRunPlan:
    return VerifyRunPlan(
        language_id="testlang",
        argv=argv,
        cwd=None,
        env={},
        report_path=report_path,
        report_adapter=report_adapter,
        report_required=report_required,
        must_include_test_sets=(),
        observation=observation or VerifyObservationPolicy(),
        report_capture=report_capture,
    )


def _py(code: str) -> tuple[str, ...]:
    """A fixture command: run this python ``code`` with the current interpreter."""
    return (sys.executable, "-c", code)


# Crafted RunnerExecution shapes -------------------------------------------------

_CLEAN = RunnerExecution(
    executed_passed_files=frozenset({"t_a.py", "t_b.py"}),
    executed_failed_files=frozenset(),
    test_level_available=True,
    total_cases=2,
    passed_cases=2,
)
_ZERO = RunnerExecution(total_cases=0)  # nothing collected, no executed files
_FAILED = RunnerExecution(
    executed_passed_files=frozenset({"t_a.py"}),
    executed_failed_files=frozenset({"t_b.py"}),
    test_level_available=True,
    total_cases=2,
    passed_cases=1,
)
# A skip is folded by the adapters into executed_failed_files (a skip proves
# nothing) — the same shape the real vitest/go adapters produce for a skipped case.
_SKIPPED = RunnerExecution(
    executed_passed_files=frozenset({"t_a.py"}),
    executed_failed_files=frozenset({"t_skip.py"}),
    test_level_available=True,
    total_cases=2,
    passed_cases=1,
)
_ONE_COLLECTED = RunnerExecution(
    executed_passed_files=frozenset({"t_a.py"}),
    executed_failed_files=frozenset(),
    test_level_available=True,
    total_cases=1,
    passed_cases=1,
)


# ── the matrix ─────────────────────────────────────────────────────────────


def test_exit0_required_report_absent_is_report_missing(tmp_path):
    # Command exits 0 but writes nothing → required report absent → REPORT_MISSING.
    plan = _plan(argv=_py("import sys; sys.exit(0)"))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.REPORT_MISSING
    assert res.returncode == 0
    assert res.execution is None


def test_exit0_report_present_but_parse_raises_is_report_unreadable(tmp_path):
    # Command writes a report; the adapter raises on parse → REPORT_UNREADABLE.
    plan = _plan(argv=_py("open('report.json','w').write('garbled'); "))
    adapter = _FakeAdapter(raises=RunnerReportUnsupported("garbled report"))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(adapter))
    assert res.verify_class is VerifyClass.REPORT_UNREADABLE
    assert res.returncode == 0


def test_exit0_report_parses_zero_tests_is_zero_tests(tmp_path):
    plan = _plan(argv=_py("open('report.json','w').write('{}')"))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_ZERO)))
    assert res.verify_class is VerifyClass.ZERO_TESTS
    assert res.returncode == 0


def test_exit0_report_with_failed_test_is_fail(tmp_path):
    plan = _plan(argv=_py("open('report.json','w').write('{}')"))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_FAILED)))
    assert res.verify_class is VerifyClass.FAIL
    assert res.returncode == 0


def test_exit0_report_with_skipped_test_is_fail(tmp_path):
    # A skip is not an authentic pass — maps to not-green (FAIL) even on exit 0.
    plan = _plan(argv=_py("open('report.json','w').write('{}')"))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_SKIPPED)))
    assert res.verify_class is VerifyClass.FAIL
    assert res.returncode == 0


def test_exit0_clean_report_is_pass(tmp_path):
    plan = _plan(argv=_py("open('report.json','w').write('{}')"))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.PASS
    assert res.is_green is True
    assert res.returncode == 0
    assert res.execution is _CLEAN


def test_nonzero_exit_beats_clean_looking_report(tmp_path):
    # Report parses clean, but the command exited nonzero → FAIL (nonzero beats green).
    plan = _plan(argv=_py("open('report.json','w').write('{}'); import sys; sys.exit(1)"))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.FAIL
    assert res.returncode == 1


def test_collected_below_min_is_zero_tests(tmp_path):
    # observation.min_collected_tests=2 but report has 1 collected → ZERO_TESTS/RED.
    plan = _plan(
        argv=_py("open('report.json','w').write('{}')"),
        observation=VerifyObservationPolicy(min_collected_tests=2),
    )
    res = execute_verify_plan(
        plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_ONE_COLLECTED))
    )
    assert res.verify_class is VerifyClass.ZERO_TESTS
    assert res.returncode == 0


def test_nonexistent_argv_is_tool_missing(tmp_path):
    plan = _plan(argv=("definitely-not-a-real-binary-xyz",))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.TOOL_MISSING
    assert res.returncode is None
    assert res.execution is None


def test_stale_green_report_is_unlinked_then_report_missing(tmp_path):
    # Pre-write a GREEN report, then run a command that exits 0 but writes NOTHING.
    # The executor must unlink the stale file first → REPORT_MISSING (proves the
    # stale green report was NOT read as this run's result).
    stale = tmp_path / "report.json"
    stale.write_text('{"green": true}', encoding="utf-8")
    plan = _plan(argv=_py("import sys; sys.exit(0)"))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.REPORT_MISSING
    assert not stale.exists()  # the stale file was removed, not left behind


def test_required_adapter_unavailable_is_report_unreadable(tmp_path):
    # plan names an adapter id that is NOT registered, report_required → the executor
    # cannot observe → REPORT_UNREADABLE/RED, never PASS (fail-closed).
    plan = _plan(argv=_py("open('report.json','w').write('{}')"), report_adapter="not-registered")
    # Registry has the fake under "fake" but NOT under "not-registered".
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.REPORT_UNREADABLE
    assert res.verify_class is not VerifyClass.PASS


# ── stdout-capture path (Go's `go test -json` shape) ───────────────────────


def test_stdout_capture_persists_report_then_classifies(tmp_path):
    # report_capture="stdout": the command prints its report to stdout and writes NO
    # file; the executor must persist stdout to report_path, then parse → PASS.
    plan = _plan(
        argv=_py("print('captured-report-line')"),
        report_path="out/report.jsonl",
        report_capture="stdout",
    )
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.PASS
    persisted = tmp_path / "out" / "report.jsonl"
    assert persisted.exists()
    assert "captured-report-line" in persisted.read_text(encoding="utf-8")


def test_stdout_capture_empty_then_parse_decides(tmp_path):
    # Even with stdout capture, if the parsed execution shows zero tests it is RED —
    # capturing stdout does NOT make an empty run green.
    plan = _plan(
        argv=_py("pass"),  # prints nothing → empty captured report
        report_path="out/report.jsonl",
        report_capture="stdout",
    )
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_ZERO)))
    assert res.verify_class is VerifyClass.ZERO_TESTS


# ── result object / green-set sanity ───────────────────────────────────────


def test_only_pass_result_is_green(tmp_path):
    plan = _plan(argv=_py("open('report.json','w').write('{}')"))
    green = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert green.is_green is True
    red = execute_verify_plan(
        _plan(argv=_py("import sys; sys.exit(0)")),
        tmp_path,
        adapter_registry=_registry_with(_FakeAdapter(_CLEAN)),
    )
    assert red.is_green is False
