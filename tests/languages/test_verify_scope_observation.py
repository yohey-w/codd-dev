"""Contract Kernel Step 5 — verify SCOPE observation (the unit-only-PASS hole).

A verify can PARSE a clean, exit-0 report and still be a FALSE green if it never
ran a test set it was REQUIRED to cover (``scope.must_include_test_sets``). This
exercises :func:`codd.languages.verify_executor.execute_verify_plan`'s scope check
DETERMINISTICALLY:

* the plan is built from a SYNTHETIC profile YAML (a tmp ``LanguageRegistry``)
  whose layout declares two test sets — ``unit`` (root ``tests/unit``) and ``e2e``
  (root ``tests/e2e``) — and whose verify command declares
  ``scope.must_include_test_sets: [unit, e2e]``; and
* a FAKE ``RunnerReportAdapter`` (as in test_verify_executor.py) returns a crafted
  :class:`RunnerExecution` whose ``executed_files`` we choose, so "which sets the
  report covered" is fully controlled.

THE INVARIANT: a required set with ZERO executed files under its root →
SCOPE_MISSING (RED), even on an exit-0 clean-looking report. A more fundamental
failure (a FAILED file) still wins over scope.
"""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from codd.languages import LanguageRegistry, build_language_contract
from codd.languages.adapters.runner_report import RunnerExecution
from codd.languages.registry import AdapterRegistry
from codd.languages.verify_executor import execute_verify_plan
from codd.languages.verify_plan import VerifyClass, build_verify_plan


# ── synthetic profiles (tmp LanguageRegistry) ──────────────────────────────

# Two explicit test sets + a verify command that REQUIRES both. The verify argv
# is a tiny python fixture (run via the current interpreter) that writes a report
# file and exits 0; the fake adapter decides what "executed_files" that report
# means, so the SCOPE outcome is what's under test, not the runner.
_PROFILE_TWO_SETS = textwrap.dedent(
    """\
    id: scopelang
    display_name: ScopeLang
    file_extensions: [".sl"]
    strictness: strict
    layout:
      source_sets:
        - id: main
          root: "src"
      test_sets:
        - id: unit
          root: "tests/unit"
        - id: e2e
          root: "tests/e2e"
      package_root:
        kind: none
    commands:
      verify:
        argv: ["PLACEHOLDER"]
        report:
          path: "report.json"
          format: "fake-json"
          adapter: "fake"
        scope:
          must_include_test_sets: [unit, e2e]
    """
)

# Same two sets, but NO scope.must_include_test_sets → scope check is a no-op.
_PROFILE_NO_SCOPE = textwrap.dedent(
    """\
    id: scopelang
    display_name: ScopeLang
    file_extensions: [".sl"]
    strictness: strict
    layout:
      test_sets:
        - id: unit
          root: "tests/unit"
        - id: e2e
          root: "tests/e2e"
      package_root:
        kind: none
    commands:
      verify:
        argv: ["PLACEHOLDER"]
        report:
          path: "report.json"
          format: "fake-json"
          adapter: "fake"
    """
)

# A COLOCATED set with root "." (tests live anywhere among sources) that the
# verify requires — any executed file trivially covers it.
_PROFILE_COLOCATED = textwrap.dedent(
    """\
    id: scopelang
    display_name: ScopeLang
    file_extensions: [".sl"]
    strictness: strict
    layout:
      test_sets:
        - id: colocated
          root: "."
          colocated: true
      package_root:
        kind: none
    commands:
      verify:
        argv: ["PLACEHOLDER"]
        report:
          path: "report.json"
          format: "fake-json"
          adapter: "fake"
        scope:
          must_include_test_sets: [colocated]
    """
)


@dataclass
class _FakeAdapter:
    """Returns a chosen RunnerExecution verbatim (controls report 'executed_files')."""

    execution: RunnerExecution

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        return self.execution


def _py(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


# Fixture command: write the (content-irrelevant) report file and exit 0. The fake
# adapter decides the parsed shape; exit 0 + a present report focuses each case on
# the scope decision.
_WRITE_REPORT_EXIT0 = _py("open('report.json','w').write('{}')")
# Same, but exit NONZERO — to prove rc!=0 (FAIL) still beats scope.
_WRITE_REPORT_EXIT1 = _py("open('report.json','w').write('{}'); import sys; sys.exit(1)")


def _build_plan(tmp_path: Path, profile_yaml: str, argv: tuple[str, ...]):
    """Write a synthetic profile, build its contract+plan, and inject ``argv``.

    ``argv`` is injected via dataclasses.replace so the same profile YAML can carry
    a clean exit-0 fixture or a nonzero one without two profile variants.
    """
    import dataclasses

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "scopelang.yaml").write_text(profile_yaml, encoding="utf-8")
    registry = LanguageRegistry(profiles_dir=profiles_dir)
    contract = build_language_contract(
        registry.resolve("scopelang"), adapter_registry=AdapterRegistry()
    )
    plan = build_verify_plan(contract)
    assert plan is not None
    return dataclasses.replace(plan, argv=argv)


def _registry_with_fake(execution: RunnerExecution) -> AdapterRegistry:
    reg = AdapterRegistry()
    reg.register("runner_report", "fake", _FakeAdapter(execution))
    return reg


def _exec(project_dir: Path, plan, execution: RunnerExecution):
    return execute_verify_plan(
        plan, project_dir, adapter_registry=_registry_with_fake(execution)
    )


# Crafted RunnerExecution shapes (executed_files = passed | failed) -------------

def _exec_files(*, passed=(), failed=()) -> RunnerExecution:
    total = len(passed) + len(failed)
    return RunnerExecution(
        executed_passed_files=frozenset(passed),
        executed_failed_files=frozenset(failed),
        test_level_available=True,
        total_cases=total,
        passed_cases=len(passed),
    )


# ── the plan carries resolved roots ────────────────────────────────────────


def test_plan_resolves_required_test_set_roots(tmp_path):
    plan = _build_plan(tmp_path, _PROFILE_TWO_SETS, _WRITE_REPORT_EXIT0)
    assert set(plan.must_include_test_sets) == {"unit", "e2e"}
    assert dict(plan.required_test_sets) == {"unit": "tests/unit", "e2e": "tests/e2e"}


# ── the scope matrix ───────────────────────────────────────────────────────


def test_both_sets_covered_clean_is_pass(tmp_path):
    # executed files under BOTH tests/unit and tests/e2e, clean → PASS.
    plan = _build_plan(tmp_path, _PROFILE_TWO_SETS, _WRITE_REPORT_EXIT0)
    execution = _exec_files(
        passed=("tests/unit/test_a.py", "tests/e2e/test_flow.py"),
    )
    res = _exec(tmp_path, plan, execution)
    assert res.verify_class is VerifyClass.PASS
    assert res.is_green is True
    assert res.returncode == 0


def test_unit_only_exit0_clean_is_scope_missing(tmp_path):
    # The historical false-green: only tests/unit ran, e2e required but absent,
    # exit 0, report clean → SCOPE_MISSING (RED), never PASS.
    plan = _build_plan(tmp_path, _PROFILE_TWO_SETS, _WRITE_REPORT_EXIT0)
    execution = _exec_files(passed=("tests/unit/test_a.py", "tests/unit/test_b.py"))
    res = _exec(tmp_path, plan, execution)
    assert res.verify_class is VerifyClass.SCOPE_MISSING
    assert res.is_green is False
    assert res.returncode == 0
    assert "e2e" in res.detail


def test_e2e_only_is_scope_missing_for_unit(tmp_path):
    # Mirror: only tests/e2e ran, unit uncovered → SCOPE_MISSING (names unit).
    plan = _build_plan(tmp_path, _PROFILE_TWO_SETS, _WRITE_REPORT_EXIT0)
    execution = _exec_files(passed=("tests/e2e/test_flow.py",))
    res = _exec(tmp_path, plan, execution)
    assert res.verify_class is VerifyClass.SCOPE_MISSING
    assert "unit" in res.detail


def test_colocated_root_dot_is_trivially_covered(tmp_path):
    # A required colocated set with root "." is covered by ANY executed file —
    # no false SCOPE_MISSING.
    plan = _build_plan(tmp_path, _PROFILE_COLOCATED, _WRITE_REPORT_EXIT0)
    assert dict(plan.required_test_sets) == {"colocated": "."}
    execution = _exec_files(passed=("anywhere/foo_test.sl",))
    res = _exec(tmp_path, plan, execution)
    assert res.verify_class is VerifyClass.PASS


def test_no_must_include_is_noop_pass(tmp_path):
    # No scope.must_include_test_sets declared → scope check is a no-op; an
    # otherwise-clean exit-0 report is PASS.
    plan = _build_plan(tmp_path, _PROFILE_NO_SCOPE, _WRITE_REPORT_EXIT0)
    assert plan.required_test_sets == ()
    execution = _exec_files(passed=("tests/unit/test_a.py",))
    res = _exec(tmp_path, plan, execution)
    assert res.verify_class is VerifyClass.PASS


def test_failed_file_beats_scope_miss(tmp_path):
    # A FAILED file AND missing e2e coverage → FAIL, not SCOPE_MISSING (a more
    # fundamental failure wins; the failed-file branch returns before scope).
    plan = _build_plan(tmp_path, _PROFILE_TWO_SETS, _WRITE_REPORT_EXIT0)
    execution = _exec_files(
        passed=("tests/unit/test_a.py",),
        failed=("tests/unit/test_b.py",),
    )
    res = _exec(tmp_path, plan, execution)
    assert res.verify_class is VerifyClass.FAIL


def test_nonzero_exit_beats_scope_miss(tmp_path):
    # rc!=0 also wins over a scope miss (rc!=0 branch returns before scope).
    plan = _build_plan(tmp_path, _PROFILE_TWO_SETS, _WRITE_REPORT_EXIT1)
    execution = _exec_files(passed=("tests/unit/test_a.py",))  # e2e uncovered
    res = _exec(tmp_path, plan, execution)
    assert res.verify_class is VerifyClass.FAIL
    assert res.returncode == 1


# ── only PASS is green (scope_missing is not green) ─────────────────────────


def test_scope_missing_is_not_green(tmp_path):
    plan = _build_plan(tmp_path, _PROFILE_TWO_SETS, _WRITE_REPORT_EXIT0)
    res = _exec(tmp_path, plan, _exec_files(passed=("tests/unit/test_a.py",)))
    assert res.verify_class is VerifyClass.SCOPE_MISSING
    assert res.is_green is False
