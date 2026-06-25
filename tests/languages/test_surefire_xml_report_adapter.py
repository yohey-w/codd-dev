"""Unit tests for ``SurefireXmlReportAdapter`` (Contract Kernel verify report) —
SYNTHETIC Maven Surefire XML (no real ``mvn`` needed). Parallels the go-test-json
adapter tests: per-file pass/taint, the skip-proves-nothing rule, the
classname→file bridge (fail-closed on an unattributable pass), directory-vs-file
report_path resolution, and Unsupported on a structurally-unreadable report.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.languages.adapters.runner_report import (
    RunnerReportUnsupported,
    SurefireXmlReportAdapter,
)


def _touch_test_class(root: Path, dotted: str) -> None:
    """Create the conventional ``src/test/java/<pkgpath>/<Class>.java`` on disk."""
    rel = root / "src" / "test" / "java" / Path(*dotted.split("."))
    rel.parent.mkdir(parents=True, exist_ok=True)
    rel.with_suffix(".java").write_text(f"// {dotted}\n", encoding="utf-8")


def _write_suite(reports: Path, classname: str, body: str) -> None:
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"TEST-{classname}.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{classname}">\n{body}\n</testsuite>\n',
        encoding="utf-8",
    )


def test_all_passing_class_is_executed_passed(tmp_path: Path) -> None:
    _touch_test_class(tmp_path, "com.example.FooTest")
    reports = tmp_path / "target" / "surefire-reports"
    _write_suite(
        reports,
        "com.example.FooTest",
        '  <testcase classname="com.example.FooTest" name="testA"/>\n'
        '  <testcase classname="com.example.FooTest" name="testB"/>',
    )
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    rel = "src/test/java/com/example/FooTest.java"
    assert rel in ex.executed_passed_files
    assert ex.executed_failed_files == frozenset()
    assert f"{rel}::com.example.FooTest#testA" in ex.executed_passed_cases
    assert ex.total_cases == 2 and ex.passed_cases == 2
    assert ex.test_level_available is True


def test_failure_taints_file(tmp_path: Path) -> None:
    _touch_test_class(tmp_path, "com.example.BarTest")
    reports = tmp_path / "target" / "surefire-reports"
    _write_suite(
        reports,
        "com.example.BarTest",
        '  <testcase classname="com.example.BarTest" name="ok"/>\n'
        '  <testcase classname="com.example.BarTest" name="bad">'
        '<failure message="boom">trace</failure></testcase>',
    )
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    rel = "src/test/java/com/example/BarTest.java"
    assert rel in ex.executed_failed_files
    assert rel not in ex.executed_passed_files


def test_error_element_taints_file(tmp_path: Path) -> None:
    _touch_test_class(tmp_path, "com.example.ErrTest")
    reports = tmp_path / "target" / "surefire-reports"
    _write_suite(
        reports,
        "com.example.ErrTest",
        '  <testcase classname="com.example.ErrTest" name="boom">'
        '<error message="npe">trace</error></testcase>',
    )
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    assert "src/test/java/com/example/ErrTest.java" in ex.executed_failed_files


def test_skipped_is_not_a_pass_and_taints_file(tmp_path: Path) -> None:
    """ANTI-FALSE-GREEN: a skipped case proves nothing — its file is tainted, never
    passed (the SAME skip rule the go-test-json/vitest adapters apply)."""
    _touch_test_class(tmp_path, "com.example.SkipTest")
    reports = tmp_path / "target" / "surefire-reports"
    _write_suite(
        reports,
        "com.example.SkipTest",
        '  <testcase classname="com.example.SkipTest" name="ran"/>\n'
        '  <testcase classname="com.example.SkipTest" name="skipped"><skipped/></testcase>',
    )
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    rel = "src/test/java/com/example/SkipTest.java"
    assert rel in ex.executed_failed_files
    assert rel not in ex.executed_passed_files
    # the skipped case is NOT a passed case key
    assert f"{rel}::com.example.SkipTest#skipped" not in ex.executed_passed_cases
    # the sibling pass IS recorded per-case (parallels Go's two-granularity design)
    assert f"{rel}::com.example.SkipTest#ran" in ex.executed_passed_cases


def test_unattributable_pass_is_not_credited(tmp_path: Path) -> None:
    """FAIL-CLOSED: a passed case whose conventional file does NOT exist on disk is
    not credited as a passed FILE (a pass you cannot attribute is never a green VB)."""
    reports = tmp_path / "target" / "surefire-reports"  # NOTE: no GhostTest.java on disk
    _write_suite(
        reports,
        "com.nope.GhostTest",
        '  <testcase classname="com.nope.GhostTest" name="t"/>',
    )
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    assert ex.executed_passed_files == frozenset()
    assert ex.executed_passed_cases == frozenset()
    # still counted so the report is not deemed empty
    assert ex.total_cases == 1 and ex.passed_cases == 1


def test_inner_class_folds_to_enclosing_file(tmp_path: Path) -> None:
    """A nested-class testcase (``Foo$Inner``) attributes to the enclosing file."""
    _touch_test_class(tmp_path, "com.example.OuterTest")
    reports = tmp_path / "target" / "surefire-reports"
    _write_suite(
        reports,
        "com.example.OuterTest",
        '  <testcase classname="com.example.OuterTest$Inner" name="nested"/>',
    )
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    assert "src/test/java/com/example/OuterTest.java" in ex.executed_passed_files


def test_report_path_may_be_a_single_file(tmp_path: Path) -> None:
    _touch_test_class(tmp_path, "com.example.FooTest")
    reports = tmp_path / "target" / "surefire-reports"
    _write_suite(
        reports,
        "com.example.FooTest",
        '  <testcase classname="com.example.FooTest" name="testA"/>',
    )
    one_file = reports / "TEST-com.example.FooTest.xml"
    ex = SurefireXmlReportAdapter().parse(one_file, project_root=tmp_path)
    assert "src/test/java/com/example/FooTest.java" in ex.executed_passed_files


def test_testsuites_wrapper_is_parsed(tmp_path: Path) -> None:
    """An aggregated ``<testsuites>`` wrapper (multiple suites) parses too."""
    _touch_test_class(tmp_path, "com.example.OneTest")
    _touch_test_class(tmp_path, "com.example.TwoTest")
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "aggregated.xml").write_text(
        '<?xml version="1.0"?>\n<testsuites>\n'
        '  <testsuite name="com.example.OneTest">'
        '<testcase classname="com.example.OneTest" name="a"/></testsuite>\n'
        '  <testsuite name="com.example.TwoTest">'
        '<testcase classname="com.example.TwoTest" name="b"/></testsuite>\n'
        "</testsuites>\n",
        encoding="utf-8",
    )
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    assert "src/test/java/com/example/OneTest.java" in ex.executed_passed_files
    assert "src/test/java/com/example/TwoTest.java" in ex.executed_passed_files
    assert ex.total_cases == 2


def test_empty_directory_raises_unsupported(tmp_path: Path) -> None:
    empty = tmp_path / "target" / "surefire-reports"
    empty.mkdir(parents=True)
    with pytest.raises(RunnerReportUnsupported):
        SurefireXmlReportAdapter().parse(empty, project_root=tmp_path)


def test_missing_path_raises_unsupported(tmp_path: Path) -> None:
    with pytest.raises(RunnerReportUnsupported):
        SurefireXmlReportAdapter().parse(
            tmp_path / "does" / "not" / "exist", project_root=tmp_path
        )


def test_unparseable_xml_only_raises_unsupported(tmp_path: Path) -> None:
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-broken.xml").write_text("<<< not xml >>>", encoding="utf-8")
    with pytest.raises(RunnerReportUnsupported):
        SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)


def test_non_testsuite_root_only_raises_unsupported(tmp_path: Path) -> None:
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-weird.xml").write_text(
        "<?xml version='1.0'?><notatestsuite><x/></notatestsuite>", encoding="utf-8"
    )
    with pytest.raises(RunnerReportUnsupported):
        SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)


def test_one_unparseable_file_tolerated_when_another_parses(tmp_path: Path) -> None:
    """A single garbled file is tolerated as long as ANOTHER file parses (mirrors the
    go-test-json tolerance of non-JSON lines) — the readable suite still counts."""
    _touch_test_class(tmp_path, "com.example.GoodTest")
    reports = tmp_path / "target" / "surefire-reports"
    _write_suite(
        reports,
        "com.example.GoodTest",
        '  <testcase classname="com.example.GoodTest" name="ok"/>',
    )
    (reports / "TEST-garbled.xml").write_text("<<< broken >>>", encoding="utf-8")
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    assert "src/test/java/com/example/GoodTest.java" in ex.executed_passed_files
