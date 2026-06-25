"""Unit tests for ``DotnetTrxReportAdapter`` (the C# ``dotnet-trx`` report parser).

Driven by SYNTHETIC VSTest TRX XML (no real ``dotnet test`` needed). Pins the
anti-false-green contract shared with the Go/vitest adapters:

* a ``.cs`` file passes only when it had ≥1 Passed case AND no Failed/Skipped/
  NotExecuted case attributed to it (a skip/NotExecuted proves nothing);
* the className→file bridge is best-effort + fail-closed (an unattributable case is
  never credited as a FILE pass);
* an unreadable / garbled / non-``TestRun`` report raises ``RunnerReportUnsupported``
  (never "nothing parseable" == "nothing ran").

The adapter is NOT registered here (the parent registers it); these tests exercise
the parser directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.languages.adapters.runner_report import (
    DotnetTrxReportAdapter,
    RunnerReportUnsupported,
)

_NS = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"


def _trx(results: str, definitions: str) -> str:
    """Wrap ``<Results>`` + ``<TestDefinitions>`` fragments in a TRX ``<TestRun>``."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<TestRun xmlns="{_NS}">\n'
        f"  <Results>\n{results}  </Results>\n"
        f"  <TestDefinitions>\n{definitions}  </TestDefinitions>\n"
        f"</TestRun>\n"
    )


def _result(test_id: str, test_name: str, outcome: str) -> str:
    return (
        f'    <UnitTestResult testId="{test_id}" testName="{test_name}" '
        f'outcome="{outcome}" />\n'
    )


def _definition(test_id: str, class_name: str, name: str) -> str:
    return (
        f'    <UnitTest id="{test_id}">\n'
        f'      <TestMethod className="{class_name}" name="{name}" />\n'
        f"    </UnitTest>\n"
    )


def _write_test_file(project_root: Path, relpath: str) -> None:
    """Create a ``*.cs`` test file so the className→file bridge can attribute a case."""
    p = project_root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("// test file\n", encoding="utf-8")


# ── pass / taint semantics ─────────────────────────────────────────────────────


def test_all_passed_file_is_passed(tmp_path: Path) -> None:
    _write_test_file(tmp_path, "tests/CalculatorTests.cs")
    trx = _trx(
        _result("g1", "CalculatorTests.Adds", "Passed")
        + _result("g2", "CalculatorTests.Subtracts", "Passed"),
        _definition("g1", "App.Tests.CalculatorTests", "Adds")
        + _definition("g2", "App.Tests.CalculatorTests", "Subtracts"),
    )
    report = tmp_path / "test.trx"
    report.write_text(trx, encoding="utf-8")

    execution = DotnetTrxReportAdapter().parse(report, project_root=tmp_path)
    assert "tests/CalculatorTests.cs" in execution.executed_passed_files
    assert execution.executed_failed_files == frozenset()
    assert execution.total_cases == 2
    assert execution.passed_cases == 2
    assert any(
        k.endswith("::App.Tests.CalculatorTests.Adds")
        for k in execution.executed_passed_cases
    ), execution.executed_passed_cases


def test_one_failed_case_taints_the_file(tmp_path: Path) -> None:
    _write_test_file(tmp_path, "tests/CalculatorTests.cs")
    trx = _trx(
        _result("g1", "CalculatorTests.Adds", "Passed")
        + _result("g2", "CalculatorTests.Subtracts", "Failed"),
        _definition("g1", "App.Tests.CalculatorTests", "Adds")
        + _definition("g2", "App.Tests.CalculatorTests", "Subtracts"),
    )
    report = tmp_path / "test.trx"
    report.write_text(trx, encoding="utf-8")

    execution = DotnetTrxReportAdapter().parse(report, project_root=tmp_path)
    assert "tests/CalculatorTests.cs" in execution.executed_failed_files
    assert "tests/CalculatorTests.cs" not in execution.executed_passed_files


@pytest.mark.parametrize("non_pass", ["NotExecuted", "Skipped"])
def test_skipped_or_notexecuted_taints_file(tmp_path: Path, non_pass: str) -> None:
    """A skip / NotExecuted proves nothing — it taints the file (Go/vitest parity)."""
    _write_test_file(tmp_path, "tests/CalculatorTests.cs")
    trx = _trx(
        _result("g1", "CalculatorTests.Adds", "Passed")
        + _result("g2", "CalculatorTests.Pending", non_pass),
        _definition("g1", "App.Tests.CalculatorTests", "Adds")
        + _definition("g2", "App.Tests.CalculatorTests", "Pending"),
    )
    report = tmp_path / "test.trx"
    report.write_text(trx, encoding="utf-8")

    execution = DotnetTrxReportAdapter().parse(report, project_root=tmp_path)
    assert "tests/CalculatorTests.cs" in execution.executed_failed_files
    assert "tests/CalculatorTests.cs" not in execution.executed_passed_files


def test_unattributable_pass_is_not_credited_as_file_pass(tmp_path: Path) -> None:
    """A passed case whose class maps to NO .cs file is NOT a FILE pass (fail-closed).

    The per-case key is still recorded (the run is not empty), but the case credits
    no ``executed_passed_files`` entry — a green we cannot attribute to a file must
    never count as a file pass.
    """
    # No matching *.cs file on disk for class "Ghosts".
    trx = _trx(
        _result("g1", "Ghosts.Vanishes", "Passed"),
        _definition("g1", "App.Tests.Ghosts", "Vanishes"),
    )
    report = tmp_path / "test.trx"
    report.write_text(trx, encoding="utf-8")

    execution = DotnetTrxReportAdapter().parse(report, project_root=tmp_path)
    assert execution.executed_passed_files == frozenset()
    assert execution.passed_cases == 1  # the case ran + passed (recorded)
    assert execution.executed_passed_cases  # under a className-derived key
    assert any("App.Tests.Ghosts" in k for k in execution.executed_passed_cases)


def test_namespaced_classname_maps_to_short_name_file(tmp_path: Path) -> None:
    """``Ns.Sub.FooTests`` attributes to a ``FooTests.cs`` (short-name join)."""
    _write_test_file(tmp_path, "tests/unit/FooTests.cs")
    trx = _trx(
        _result("g1", "FooTests.Works", "Passed"),
        _definition("g1", "App.Deeply.Nested.FooTests", "Works"),
    )
    report = tmp_path / "test.trx"
    report.write_text(trx, encoding="utf-8")

    execution = DotnetTrxReportAdapter().parse(report, project_root=tmp_path)
    assert "tests/unit/FooTests.cs" in execution.executed_passed_files


# ── unreadable / garbled → RunnerReportUnsupported (never an empty pass) ────────


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RunnerReportUnsupported):
        DotnetTrxReportAdapter().parse(tmp_path / "nope.trx", project_root=tmp_path)


def test_garbled_xml_raises(tmp_path: Path) -> None:
    report = tmp_path / "test.trx"
    report.write_text("<TestRun><Results> not closed", encoding="utf-8")
    with pytest.raises(RunnerReportUnsupported):
        DotnetTrxReportAdapter().parse(report, project_root=tmp_path)


def test_non_testrun_root_raises(tmp_path: Path) -> None:
    report = tmp_path / "test.trx"
    report.write_text(
        f'<SomethingElse xmlns="{_NS}"><Results/></SomethingElse>', encoding="utf-8"
    )
    with pytest.raises(RunnerReportUnsupported):
        DotnetTrxReportAdapter().parse(report, project_root=tmp_path)


def test_testrun_with_no_results_raises(tmp_path: Path) -> None:
    """A ``<TestRun>`` carrying zero ``<UnitTestResult>`` is unreadable, not empty-pass."""
    report = tmp_path / "test.trx"
    report.write_text(
        f'<TestRun xmlns="{_NS}"><Results></Results>'
        f"<TestDefinitions></TestDefinitions></TestRun>",
        encoding="utf-8",
    )
    with pytest.raises(RunnerReportUnsupported):
        DotnetTrxReportAdapter().parse(report, project_root=tmp_path)
