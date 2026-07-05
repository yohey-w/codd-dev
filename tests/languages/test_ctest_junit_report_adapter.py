"""Unit tests for ``CTestJunitReportAdapter`` (Contract Kernel verify report) —
SYNTHETIC CTest JUnit XML (no real ``ctest`` needed). Parallels the surefire-xml
adapter tests, but the C++ join key is the GoogleTest ``Suite.Case`` (or Catch2
case) LABEL rather than a filename stem: each ``<testcase name="Suite.Case">`` is
attributed to the real ``.cpp``/``.cc``/``.cxx`` test file that statically defines
that macro (:func:`_cpp_test_label_index`, the C++ arm of the IDENTITY→FILE
ATTRIBUTION NORM). Covers per-file pass/taint, the skip-proves-nothing rule, the
value-/type-parameterized (``TEST_P``) name normalization, Catch2 names, the
compiled-but-zero-tests file credited to nothing, the fail-closed unattributed
case, the ``<testsuites>`` wrapper, and Unsupported on an unreadable report.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.languages.adapters.runner_report import (
    CTestJunitReportAdapter,
    RunnerReportUnsupported,
    _ctest_case_label_candidates,
    _cpp_test_label_index,
)


def _write(path: Path, body: str, *, root: str = "testsuite") -> Path:
    """Write a synthetic CTest JUnit report at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<{root} name="ctest">\n{body}\n</{root}>\n',
        encoding="utf-8",
    )
    return path


def _write_cpp(root: Path, rel: str, source: str) -> None:
    """Write a real (minimal) C++ test source at ``rel`` under ``root``.

    The tree-scanned index parses the file's OWN ``TEST(...)`` / ``TEST_CASE(...)``
    macros to learn its (suite, case) labels — so a bare comment is not enough; the
    macro must really be present for the label to be indexed.
    """
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


# ── candidate-key normalization (pure) ────────────────────────────────────────


def test_candidate_keys_plain_case_is_raw() -> None:
    assert _ctest_case_label_candidates("Suite.Case") == ["Suite.Case"]


def test_candidate_keys_instantiated_value_param() -> None:
    """``INSTANTIATE_TEST_SUITE_P`` decorates the name with a prefix + ``/index``;
    the suite-last-segment + case-first-segment candidate recovers ``Suite.Case``."""
    cands = _ctest_case_label_candidates("Inst/Suite.Case/0")
    assert cands[0] == "Inst/Suite.Case/0"  # raw first
    assert "Suite.Case" in cands


def test_candidate_keys_value_param_without_prefix() -> None:
    assert "Suite.Case" in _ctest_case_label_candidates("Suite.Case/3")


def test_candidate_keys_type_param_index_in_suite() -> None:
    """``TYPED_TEST`` puts the ``/index`` in the SUITE part (``Suite/0.Case``); the
    suite-first-segment + case-first-segment candidate recovers ``Suite.Case``."""
    assert "Suite.Case" in _ctest_case_label_candidates("Suite/0.Case")


def test_candidate_keys_catch2_name_is_raw() -> None:
    """A Catch2 case name (arbitrary string) matches on the raw candidate."""
    assert _ctest_case_label_candidates("parses a valid expression") == [
        "parses a valid expression"
    ]


# ── file attribution (the reversed norm — files are now POPULATED) ────────────


def test_all_passing_cases_attribute_to_their_file(tmp_path: Path) -> None:
    """REVERSED from the old ``…files_empty``: passing cases whose (suite, case) label
    is statically defined in a real ``.cc`` now attribute that FILE into
    ``executed_passed_files`` (the C++ IDENTITY→FILE ATTRIBUTION NORM), while the
    per-case keys are recorded as ``<relfile>::<name>``."""
    _write_cpp(
        tmp_path,
        "tests/math_test.cc",
        "#include <gtest/gtest.h>\n"
        "TEST(MathTest, Adds) { EXPECT_EQ(2, 1 + 1); }\n"
        "TEST(MathTest, Subtracts) { EXPECT_EQ(0, 1 - 1); }\n",
    )
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="MathTest" name="MathTest.Adds" time="0.01"/>\n'
        '  <testcase classname="MathTest" name="MathTest.Subtracts" time="0.01"/>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert ex.executed_passed_files == frozenset({"tests/math_test.cc"})
    assert ex.executed_failed_files == frozenset()
    assert "tests/math_test.cc::MathTest.Adds" in ex.executed_passed_cases
    assert "tests/math_test.cc::MathTest.Subtracts" in ex.executed_passed_cases
    assert ex.total_cases == 2 and ex.passed_cases == 2
    assert ex.test_level_available is True


def test_suite_name_differs_from_filename_still_attributes(tmp_path: Path) -> None:
    """The join key is the gtest ``Suite.Case`` LABEL, NOT the filename — so a file
    named ``errors_test.cc`` whose suite is ``ErrorHierarchyTest`` still attributes
    (this is exactly what a filename-stem index like Java/C# could NOT do)."""
    _write_cpp(
        tmp_path,
        "tests/errors_test.cc",
        "#include <gtest/gtest.h>\n"
        "#include <stdexcept>\n"
        "TEST(ErrorHierarchyTest, ExprErrorDerivesFromStdException) {\n"
        "  EXPECT_TRUE(true);\n"
        "}\n",
    )
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="ErrorHierarchyTest" '
        'name="ErrorHierarchyTest.ExprErrorDerivesFromStdException" time="0.01"/>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert ex.executed_passed_files == frozenset({"tests/errors_test.cc"})
    assert (
        "tests/errors_test.cc::ErrorHierarchyTest.ExprErrorDerivesFromStdException"
        in ex.executed_passed_cases
    )


def test_test_p_decorated_name_attributes(tmp_path: Path) -> None:
    """A value-parameterized ``TEST_P(ParamSuite, HandlesValue)`` is reported by ctest
    as ``Inst/ParamSuite.HandlesValue/0``; the candidate normalization strips the
    instantiation prefix + index so it joins the static ``ParamSuite.HandlesValue``."""
    _write_cpp(
        tmp_path,
        "tests/param_test.cc",
        "#include <gtest/gtest.h>\n"
        "TEST_P(ParamSuite, HandlesValue) { EXPECT_GE(GetParam(), 0); }\n",
    )
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="Inst/ParamSuite" '
        'name="Inst/ParamSuite.HandlesValue/0" time="0.01"/>\n'
        '  <testcase classname="Inst/ParamSuite" '
        'name="Inst/ParamSuite.HandlesValue/1" time="0.01"/>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert ex.executed_passed_files == frozenset({"tests/param_test.cc"})
    assert ex.total_cases == 2 and ex.passed_cases == 2


def test_catch2_case_name_attributes(tmp_path: Path) -> None:
    """A Catch2 ``TEST_CASE("...")`` is labelled by its case name; ctest reports that
    exact string, so the raw candidate joins it to its file."""
    _write_cpp(
        tmp_path,
        "tests/parser_test.cc",
        '#include <catch2/catch_test_macros.hpp>\n'
        'TEST_CASE("parses a valid expression", "[parser]") { REQUIRE(1 == 1); }\n',
    )
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase name="parses a valid expression" time="0.01"/>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert ex.executed_passed_files == frozenset({"tests/parser_test.cc"})


def test_compiled_but_zero_tests_file_credited_to_nothing(tmp_path: Path) -> None:
    """A test source that compiles but defines ZERO test macros contributes no label
    to the index, so it is NEVER credited — even though a sibling file passes."""
    _write_cpp(
        tmp_path,
        "tests/math_test.cc",
        "#include <gtest/gtest.h>\nTEST(MathTest, Adds) { EXPECT_EQ(2, 1 + 1); }\n",
    )
    _write_cpp(
        tmp_path,
        "tests/zero_cases_test.cc",
        "#include <gtest/gtest.h>\n// helpers only, no TEST(...) macro here\n"
        "int helper() { return 0; }\n",
    )
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="MathTest" name="MathTest.Adds" time="0.01"/>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert "tests/math_test.cc" in ex.executed_passed_files
    assert "tests/zero_cases_test.cc" not in ex.executed_files


def test_unattributed_case_is_fail_closed(tmp_path: Path) -> None:
    """FAIL-CLOSED: a passed case whose label joins no discovered file is counted in
    the totals (so the report is not deemed empty) but is credited to NO file and
    contributes NO passed-case key (mirrors Go's parser-miss discipline)."""
    # a real file exists, but the report names a case that file does NOT define
    _write_cpp(
        tmp_path,
        "tests/math_test.cc",
        "#include <gtest/gtest.h>\nTEST(MathTest, Adds) { EXPECT_EQ(2, 1 + 1); }\n",
    )
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="GhostSuite" name="GhostSuite.NeverDefined" time="0.01"/>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert ex.executed_passed_files == frozenset()
    assert ex.executed_failed_files == frozenset()
    assert ex.executed_passed_cases == frozenset()
    assert ex.total_cases == 1 and ex.passed_cases == 1  # still counted


def test_skip_pollutes_its_file(tmp_path: Path) -> None:
    """ANTI-FALSE-GREEN: a skipped case proves nothing — its FILE is tainted into
    ``executed_failed_files`` even though a sibling case in the same file passed."""
    _write_cpp(
        tmp_path,
        "tests/math_test.cc",
        "#include <gtest/gtest.h>\n"
        "TEST(MathTest, Adds) { EXPECT_EQ(2, 1 + 1); }\n"
        "TEST(MathTest, Skipped) { GTEST_SKIP(); }\n",
    )
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="MathTest" name="MathTest.Adds"/>\n'
        '  <testcase classname="MathTest" name="MathTest.Skipped"><skipped/></testcase>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert "tests/math_test.cc" in ex.executed_failed_files
    assert "tests/math_test.cc" not in ex.executed_passed_files
    # the skipped case is not a passed-case key; the sibling pass still is
    assert "tests/math_test.cc::MathTest.Skipped" not in ex.executed_passed_cases
    assert "tests/math_test.cc::MathTest.Adds" in ex.executed_passed_cases


def test_failure_pollutes_its_file(tmp_path: Path) -> None:
    """A ``<failure>`` case taints its whole file into ``executed_failed_files``."""
    _write_cpp(
        tmp_path,
        "tests/math_test.cc",
        "#include <gtest/gtest.h>\n"
        "TEST(MathTest, Ok) { EXPECT_TRUE(true); }\n"
        "TEST(MathTest, Bad) { EXPECT_TRUE(false); }\n",
    )
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="MathTest" name="MathTest.Ok"/>\n'
        '  <testcase classname="MathTest" name="MathTest.Bad">'
        '<failure message="boom">trace</failure></testcase>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert "tests/math_test.cc" in ex.executed_failed_files
    assert "tests/math_test.cc" not in ex.executed_passed_files


def test_error_child_pollutes_its_file(tmp_path: Path) -> None:
    _write_cpp(
        tmp_path,
        "tests/crash_test.cc",
        "#include <gtest/gtest.h>\nTEST(CrashTest, Boom) { EXPECT_TRUE(true); }\n",
    )
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="CrashTest" name="CrashTest.Boom">'
        '<error message="segv">trace</error></testcase>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert "tests/crash_test.cc" in ex.executed_failed_files
    assert ex.passed_cases == 0


def test_unnamed_case_is_skipped(tmp_path: Path) -> None:
    """A testcase with no name identifies nothing — it is skipped (credits/counts
    nothing); only the named case is counted."""
    _write_cpp(
        tmp_path,
        "tests/math_test.cc",
        "#include <gtest/gtest.h>\nTEST(MathTest, Real) { EXPECT_TRUE(true); }\n",
    )
    report = _write(
        tmp_path / "build" / "ctest-junit.xml",
        '  <testcase classname="MathTest" time="0.0"/>\n'
        '  <testcase classname="MathTest" name="MathTest.Real"/>',
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert ex.total_cases == 1  # only the named case counted
    assert "tests/math_test.cc" in ex.executed_passed_files


def test_testsuites_wrapper_is_parsed(tmp_path: Path) -> None:
    """An aggregated ``<testsuites>`` wrapper around multiple ``<testsuite>`` parses,
    and each case attributes to its file."""
    _write_cpp(
        tmp_path,
        "tests/a_test.cc",
        "#include <gtest/gtest.h>\nTEST(ASuite, X) { EXPECT_TRUE(true); }\n",
    )
    _write_cpp(
        tmp_path,
        "tests/b_test.cc",
        "#include <gtest/gtest.h>\nTEST(BSuite, Y) { EXPECT_TRUE(true); }\n",
    )
    report = tmp_path / "build" / "ctest-junit.xml"
    report.parent.mkdir(parents=True)
    report.write_text(
        '<?xml version="1.0"?>\n<testsuites>\n'
        '  <testsuite name="A"><testcase classname="ASuite" name="ASuite.X"/></testsuite>\n'
        '  <testsuite name="B"><testcase classname="BSuite" name="BSuite.Y"/></testsuite>\n'
        "</testsuites>\n",
        encoding="utf-8",
    )
    ex = CTestJunitReportAdapter().parse(report, project_root=tmp_path)
    assert "tests/a_test.cc" in ex.executed_passed_files
    assert "tests/b_test.cc" in ex.executed_passed_files
    assert ex.total_cases == 2


def test_index_first_wins_on_duplicate_label(tmp_path: Path) -> None:
    """A (practically impossible in a compiling project) duplicate label resolves
    deterministically to the first file in sorted-path order — the SAME tie-break
    the Surefire/TRX indexes use."""
    _write_cpp(
        tmp_path,
        "aaa/dup_test.cc",
        "#include <gtest/gtest.h>\nTEST(DupSuite, Case) { EXPECT_TRUE(true); }\n",
    )
    _write_cpp(
        tmp_path,
        "zzz/dup_test.cc",
        "#include <gtest/gtest.h>\nTEST(DupSuite, Case) { EXPECT_TRUE(true); }\n",
    )
    index = _cpp_test_label_index(tmp_path)
    assert index["DupSuite.Case"] == "aaa/dup_test.cc"


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
