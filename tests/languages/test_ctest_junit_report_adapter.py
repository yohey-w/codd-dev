"""Unit tests for ``CTestJunitReportAdapter`` (Contract Kernel verify report) —
SYNTHETIC CTest JUnit XML (no real ``ctest`` needed). Parallels the surefire-xml
adapter tests: per-case pass/skip/fail, the skip-proves-nothing rule, the
documented FILE-level limitation (ctest case names do not map to a .cpp file, so
executed_passed_files stays EMPTY while executed_passed_cases is populated), the
``<testsuites>`` wrapper, and Unsupported on a structurally-unreadable report.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.languages.adapters.runner_report import (
    CTestJunitReportAdapter,
    RunnerReportUnsupported,
)


def _write(path: Path, body: str, *, root: str = "testsuite") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<{root} name="ctest">\n{body}\n</{root}>\n',
        encoding="utf-8",
    )
    return path


def test_all_passing_cases_are_passed_cases_files_empty(tmp_path: Path) -> None:
    """Passing cases populate executed_passed_cases; executed_passed_files stays EMPTY.

    The documented FILE-level limitation: ctest case names do not map to a real .cpp
    file on disk, so we never fabricate a passed FILE (fail-closed) — but the per-case
    identities ARE recorded (the reconciliation authority).
    """
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="MathTest" name="MathTest.Adds" time="0.01"/>\n'
        '  <testcase classname="MathTest" name="MathTest.Subtracts" time="0.01"/>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert ex.executed_passed_files == frozenset()
    assert ex.executed_failed_files == frozenset()
    assert "MathTest::MathTest.Adds" in ex.executed_passed_cases
    assert "MathTest::MathTest.Subtracts" in ex.executed_passed_cases
    assert ex.total_cases == 2 and ex.passed_cases == 2
    assert ex.test_level_available is True


def test_failure_case_is_not_a_passed_case(tmp_path: Path) -> None:
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="T" name="T.ok"/>\n'
        '  <testcase classname="T" name="T.bad"><failure message="boom">trace</failure></testcase>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert "T::T.ok" in ex.executed_passed_cases
    assert "T::T.bad" not in ex.executed_passed_cases
    assert ex.total_cases == 2 and ex.passed_cases == 1


def test_error_child_is_not_a_passed_case(tmp_path: Path) -> None:
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="T" name="T.boom"><error message="segv">trace</error></testcase>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert ex.executed_passed_cases == frozenset()
    assert ex.total_cases == 1 and ex.passed_cases == 0


def test_skipped_is_not_a_pass(tmp_path: Path) -> None:
    """ANTI-FALSE-GREEN: a skipped case proves nothing — never a passed case (the SAME
    skip rule the go-test-json/vitest/surefire adapters apply)."""
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="T" name="T.ran"/>\n'
        '  <testcase classname="T" name="T.skip"><skipped/></testcase>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert "T::T.ran" in ex.executed_passed_cases
    assert "T::T.skip" not in ex.executed_passed_cases
    assert ex.total_cases == 2 and ex.passed_cases == 1


def test_case_key_falls_back_to_name_without_classname(tmp_path: Path) -> None:
    """With no classname (a bare ctest test, not a GoogleTest suite), the key uses the
    test name on both sides of ``::``."""
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase name="integration_smoke" time="1.2"/>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert "integration_smoke::integration_smoke" in ex.executed_passed_cases


def test_unnamed_case_is_skipped(tmp_path: Path) -> None:
    """A testcase with no name identifies nothing — it is skipped (credits nothing)."""
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="T" time="0.0"/>\n'
        '  <testcase classname="T" name="T.real"/>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert ex.total_cases == 1  # only the named case counted
    assert "T::T.real" in ex.executed_passed_cases


def test_testsuites_wrapper_is_parsed(tmp_path: Path) -> None:
    """An aggregated ``<testsuites>`` wrapper around multiple ``<testsuite>`` parses."""
    report = tmp_path / "build" / "ctest-junit.xml"
    report.parent.mkdir(parents=True)
    report.write_text(
        '<?xml version="1.0"?>\n<testsuites>\n'
        '  <testsuite name="A"><testcase classname="A" name="A.x"/></testsuite>\n'
        '  <testsuite name="B"><testcase classname="B" name="B.y"/></testsuite>\n'
        "</testsuites>\n",
        encoding="utf-8",
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert "A::A.x" in ex.executed_passed_cases
    assert "B::B.y" in ex.executed_passed_cases
    assert ex.total_cases == 2


def test_missing_file_raises_unsupported(tmp_path: Path) -> None:
    with pytest.raises(RunnerReportUnsupported):
        CTestJunitReportAdapter().parse(
            tmp_path / "build" / "ctest-junit.xml", project_root=tmp_path
        )


def test_unparseable_xml_raises_unsupported(tmp_path: Path) -> None:
    report = tmp_path / "build" / "ctest-junit.xml"
    report.parent.mkdir(parents=True)
    report.write_text("<<< not xml >>>", encoding="utf-8")
    with pytest.raises(RunnerReportUnsupported):
        CTestJunitReportAdapter().parse(report, project_root=tmp_path)


def test_non_testsuite_root_raises_unsupported(tmp_path: Path) -> None:
    report = tmp_path / "build" / "ctest-junit.xml"
    report.parent.mkdir(parents=True)
    report.write_text(
        "<?xml version='1.0'?><notasuite><x/></notasuite>", encoding="utf-8"
    )
    with pytest.raises(RunnerReportUnsupported):
        CTestJunitReportAdapter().parse(report, project_root=tmp_path)
