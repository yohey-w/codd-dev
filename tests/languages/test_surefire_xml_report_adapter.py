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
    """Create the conventional ``src/test/java/<pkgpath>/<Class>.java`` on disk.

    Writes a REAL (if minimal) Java file — a ``package`` declaration matching the
    dotted FQCN plus a class stub — because the tree-scanned attribution index
    (:func:`_surefire_class_file_index`) reads the file's OWN ``package`` line
    rather than assuming the conventional path encodes it; a bare comment is not a
    real Java file and would leave the index unable to find it.
    """
    rel = root / "src" / "test" / "java" / Path(*dotted.split("."))
    rel.parent.mkdir(parents=True, exist_ok=True)
    parts = dotted.split(".")
    class_name = parts[-1]
    package = ".".join(parts[:-1])
    header = f"package {package};\n\n" if package else ""
    rel.with_suffix(".java").write_text(
        f"{header}public class {class_name} {{\n}}\n", encoding="utf-8"
    )


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


# ── Tree-scan attribution (2026-07-02 rewrite: identity→file via a real tree scan,
# never a src/test/java path template) ───────────────────────────────────────────


def _write_java_file(root: Path, rel: str, *, package: str | None, class_name: str) -> None:
    """Write a real (minimal) ``.java`` file at ``rel`` with an explicit package line.

    Unlike :func:`_touch_test_class`, ``rel`` need not mirror the Maven
    ``src/test/java`` convention — this is exactly the point: a real file's
    ``package`` declaration is independent of which root it physically lives
    under, which is what lets the tree-scanned index attribute a class that a
    single-root path TEMPLATE could never find.
    """
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"package {package};\n\n" if package else ""
    path.write_text(f"{header}public class {class_name} {{\n}}\n", encoding="utf-8")


def test_non_standard_root_class_is_attributed_via_tree_scan(tmp_path: Path) -> None:
    """THE LATENT-BUG REGRESSION TEST: a test class that does NOT live under
    ``src/test/java`` (e.g. a second declared test root, such as a Failsafe-run
    ``tests/e2e/java/**`` integration-test tree) still attributes to its REAL file,
    because the index is built from a scan of the actual tree + each file's own
    ``package`` declaration — never a templated ``src/test/java/{pkg}/{cls}.java``
    guess. The OLD template-based adapter could never find this file (it would
    only ever look under ``src/test/java``), so this exact case was a silent
    fail-closed misattribution before the tree-scan rewrite."""
    _write_java_file(
        tmp_path,
        "tests/e2e/java/com/example/e2e/ParseE2EIT.java",
        package="com.example.e2e",
        class_name="ParseE2EIT",
    )
    reports = tmp_path / "target" / "failsafe-reports"
    _write_suite(
        reports,
        "com.example.e2e.ParseE2EIT",
        '  <testcase classname="com.example.e2e.ParseE2EIT" name="parsesOk"/>',
    )
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    rel = "tests/e2e/java/com/example/e2e/ParseE2EIT.java"
    assert rel in ex.executed_passed_files
    assert f"{rel}::com.example.e2e.ParseE2EIT#parsesOk" in ex.executed_passed_cases


def test_unit_and_e2e_roots_with_distinct_packages_both_attribute(tmp_path: Path) -> None:
    """Two classes in TWO different declared roots (unit under ``src/test/java``,
    e2e under ``tests/e2e/java``) with DISTINCT packages both attribute correctly
    from ONE report — the 1-step×2-report Java shape's file-bridge, at the
    adapter level (the campaign-level merge is exercised separately)."""
    _touch_test_class(tmp_path, "com.example.unit.FooTest")
    _write_java_file(
        tmp_path,
        "tests/e2e/java/com/example/e2e/BarE2EIT.java",
        package="com.example.e2e",
        class_name="BarE2EIT",
    )
    reports = tmp_path / "target" / "merged-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-com.example.unit.FooTest.xml").write_text(
        '<?xml version="1.0"?><testsuite name="com.example.unit.FooTest">'
        '<testcase classname="com.example.unit.FooTest" name="a"/></testsuite>',
        encoding="utf-8",
    )
    (reports / "TEST-com.example.e2e.BarE2EIT.xml").write_text(
        '<?xml version="1.0"?><testsuite name="com.example.e2e.BarE2EIT">'
        '<testcase classname="com.example.e2e.BarE2EIT" name="b"/></testsuite>',
        encoding="utf-8",
    )
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    assert "src/test/java/com/example/unit/FooTest.java" in ex.executed_passed_files
    assert "tests/e2e/java/com/example/e2e/BarE2EIT.java" in ex.executed_passed_files
    assert ex.total_cases == 2


def test_default_package_class_attributes_by_bare_stem(tmp_path: Path) -> None:
    """A class with NO ``package`` declaration (Java's default package) keys on its
    bare stem — the index's fallback for the (unusual, but legal) unnamed-package
    case, rather than silently failing to index the file at all."""
    _write_java_file(tmp_path, "tests/NoPackageTest.java", package=None, class_name="NoPackageTest")
    reports = tmp_path / "target" / "surefire-reports"
    _write_suite(reports, "NoPackageTest", '  <testcase classname="NoPackageTest" name="ok"/>')
    ex = SurefireXmlReportAdapter().parse(reports, project_root=tmp_path)
    assert "tests/NoPackageTest.java" in ex.executed_passed_files


def test_duplicate_fqcn_across_two_files_first_sorted_path_wins(tmp_path: Path) -> None:
    """A (build-breaking, hence practically impossible) duplicate FQCN resolves
    deterministically to the first file in sorted-path order — mirroring the
    tie-break :func:`_trx_cs_file_index` (C#) already uses."""
    _write_java_file(
        tmp_path, "aaa/com/example/DupTest.java", package="com.example", class_name="DupTest"
    )
    _write_java_file(
        tmp_path, "zzz/com/example/DupTest.java", package="com.example", class_name="DupTest"
    )
    from codd.languages.adapters.runner_report import _surefire_class_file_index

    index = _surefire_class_file_index(tmp_path)
    assert index["com.example.DupTest"] == "aaa/com/example/DupTest.java"
