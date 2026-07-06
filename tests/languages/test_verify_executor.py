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
        required_test_sets=(),
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


def test_unsubstituted_test_root_placeholder_refuses_to_spawn(tmp_path):
    # REGRESSION (dogfood ExprCalcTs): a plan whose argv still carries a literal
    # {test_root}/{report} template placeholder (e.g. build_verify_plan was skipped,
    # or resolved an ambiguous test root) must NOT be spawned — that is the v2.75
    # cwd-bug class, applied to the core verify plan. The fixture command below
    # would otherwise exit 0 and write a clean report, so a false PASS here would
    # not be an accident of the command simply failing to run.
    plan = _plan(argv=_py("open('report.json','w').write('{}')") + ("{test_root}",))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.CONFIG_ERROR
    assert res.returncode is None
    assert "unsubstituted" in res.detail.lower()
    assert "{test_root}" in res.detail


def test_unsubstituted_report_placeholder_refuses_to_spawn(tmp_path):
    plan = _plan(argv=_py("open('report.json','w').write('{}')") + ("--outputFile={report}",))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.CONFIG_ERROR
    assert res.returncode is None


def test_resolved_argv_with_concrete_test_root_and_report_still_passes(tmp_path):
    # Sanity / no-regression: a FULLY substituted plan (the normal case) must still
    # reach PASS — the new guard must not false-flag a legitimate concrete argv.
    plan = _plan(argv=_py("open('report.json','w').write('{}')"))
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.PASS


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


def test_stale_report_directory_with_content_is_removed_then_report_missing(tmp_path):
    # REGRESSION (dogfood java_v2, ExprCalc): some runners write a DIRECTORY of
    # per-file reports, not a single file — e.g. Maven Surefire's
    # `target/surefire-reports/` holds one XML/txt pair per test class. A bare
    # `Path.unlink()` only removes FILES: called on an existing, non-empty
    # report DIRECTORY it raises `IsADirectoryError` (an `OSError` subclass),
    # which used to be swallowed and misreported as "could not remove stale
    # report" — a permanent false RED on every subsequent verify run, even with
    # completely ordinary ownership/permissions. Pre-populate report_path as a
    # real, non-empty directory (mirrors an ordinary prior `mvn test` output)
    # and assert it is removed CLEANLY and the run PROCEEDS (reaches the next
    # legitimate not-green gate — a required report legitimately absent because
    # this run's command wrote nothing — rather than erroring on removal).
    stale_dir = tmp_path / "reports"
    stale_dir.mkdir()
    (stale_dir / "TEST-old-a.xml").write_text("<testsuite/>", encoding="utf-8")
    (stale_dir / "TEST-old-b.xml").write_text("<testsuite/>", encoding="utf-8")
    plan = _plan(argv=_py("import sys; sys.exit(0)"), report_path="reports")
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.REPORT_MISSING
    assert "could not remove" not in res.detail
    assert not stale_dir.exists()  # the stale directory was removed, not left behind


def test_stale_report_directory_is_removed_then_fresh_directory_report_passes(tmp_path):
    # Full round-trip: a stale, POPULATED report directory exists before the run
    # (as Maven Surefire's target/surefire-reports/ does across repeated `mvn
    # test` invocations), and the command recreates the SAME directory path with
    # fresh content. The run must reach PASS via the fresh content only, and the
    # old file must be gone — proving the stale directory was actually removed
    # (not merely tolerated / left to coexist with the new one).
    stale_dir = tmp_path / "reports"
    stale_dir.mkdir()
    (stale_dir / "TEST-old.xml").write_text("<testsuite/>", encoding="utf-8")
    write_fresh_report = (
        "import os; os.makedirs('reports', exist_ok=True); "
        "open('reports/TEST-new.xml', 'w').write('<testsuite/>')"
    )
    plan = _plan(argv=_py(write_fresh_report), report_path="reports")
    res = execute_verify_plan(plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN)))
    assert res.verify_class is VerifyClass.PASS
    assert [p.name for p in stale_dir.iterdir()] == ["TEST-new.xml"]


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


# ── exec-path prepend (language-agnostic PATH resolution seam, v3.15.0) ─────────
#
# A caller (the verify runner reading a harness-owned env-provision state artifact)
# can hand the executor a list of REAL absolute directories to prepend to the
# spawn's PATH. The executor knows NOTHING of what lives there — it only prepends
# existence-checked dirs so an unchanged bare ``argv[0]`` resolves to that dir's
# binary. With NO dirs (the default) the spawn env is byte-identical to today.


def _fake_tool_bin(tmp_path, *, name: str) -> Path:
    """A dir holding an executable ``name`` that writes an empty report and exits 0."""
    bindir = tmp_path / "toolbin"
    bindir.mkdir(exist_ok=True)
    tool = bindir / name
    tool.write_text("#!/bin/sh\nprintf '{}' > report.json\nexit 0\n")
    tool.chmod(0o755)
    return bindir


def test_exec_path_prepend_resolves_bare_argv0_to_prepended_dir(tmp_path):
    # RED-1: a uniquely-named tool that is NOT on PATH.
    bindir = _fake_tool_bin(tmp_path, name="codd_fake_verify_tool")
    plan = _plan(argv=("codd_fake_verify_tool",))
    # Without the prepend, the bare tool cannot spawn → TOOL_MISSING (proves the
    # prepend is what resolves it, not an ambient PATH entry).
    missing = execute_verify_plan(
        plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN))
    )
    assert missing.verify_class is VerifyClass.TOOL_MISSING
    # With the prepend, the bare argv[0] resolves to the prepended dir's binary → PASS.
    res = execute_verify_plan(
        plan,
        tmp_path,
        adapter_registry=_registry_with(_FakeAdapter(_CLEAN)),
        exec_path_prepend=(str(bindir),),
    )
    assert res.verify_class is VerifyClass.PASS
    assert res.returncode == 0


def test_exec_path_prepend_ignores_nonexistent_dir(tmp_path):
    # A forged / stale state artifact pointing at a dir that does not exist must be
    # DROPPED by the existence check — the bare tool then stays unresolvable →
    # TOOL_MISSING (never a silent pass from a bogus prepend).
    plan = _plan(argv=("codd_fake_verify_tool",))
    res = execute_verify_plan(
        plan,
        tmp_path,
        adapter_registry=_registry_with(_FakeAdapter(_CLEAN)),
        exec_path_prepend=(str(tmp_path / "does" / "not" / "exist"),),
    )
    assert res.verify_class is VerifyClass.TOOL_MISSING


def test_empty_exec_path_prepend_is_byte_identical(tmp_path, monkeypatch):
    # Byte-identity: the default (no prepend) never touches PATH. A tool reachable
    # only via the ambient PATH still resolves; passing exec_path_prepend=() must
    # behave exactly like omitting it.
    bindir = _fake_tool_bin(tmp_path, name="codd_fake_verify_tool2")
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    plan = _plan(argv=("codd_fake_verify_tool2",))
    default = execute_verify_plan(
        plan, tmp_path, adapter_registry=_registry_with(_FakeAdapter(_CLEAN))
    )
    empty = execute_verify_plan(
        plan,
        tmp_path,
        adapter_registry=_registry_with(_FakeAdapter(_CLEAN)),
        exec_path_prepend=(),
    )
    assert default.verify_class is VerifyClass.PASS
    assert empty.verify_class is VerifyClass.PASS
