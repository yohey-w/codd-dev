"""Unit + end-to-end tests for :class:`CppTestBlockProfile`.

The C++ (GoogleTest / Catch2) structural adapter is exercised directly (parse,
skip detection, direct constant-vs-real assertion classification) and through the
full :func:`build_authenticity_report` gate with a profile stub, mirroring the
anti-false-green conformance contract in ``tests/test_profile_conformance.py``.
The cardinal assertion is the SAME as the conformance contract: a FAKE covering
test (no assertion / constant-only / skipped) MUST be rejected, a real covering
test MUST be credited.
"""
from __future__ import annotations

import pytest

from codd.vb_marker_authenticity import (
    CppTestBlockProfile,
    build_authenticity_report,
)

_MARKER = "// codd: covers vb=VB-01"


# ---------------------------------------------------------------------------
# handles_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path,expected",
    [
        ("tests/x_test.cpp", True),          # the conformance fixture path
        ("tests/foo.cc", True),              # under tests/ (no "test" in basename)
        ("tests/bar.cxx", True),
        ("test/baz.cpp", True),              # singular test/ dir
        ("src/FooTests.cpp", True),          # "test" in basename (CamelCase)
        ("src/test_foo.cc", True),           # leading test_
        ("a/b/widget_test.cpp", True),       # nested
        ("src/main.cpp", False),             # ordinary source — not a test
        ("src/app.cc", False),
        ("tests/x_test.h", False),           # headers carry no executable TEST(...)
        ("tests/x_test.hpp", False),
        ("tests/x_test.py", False),          # wrong language
        ("", False),
    ],
)
def test_handles_file(rel_path: str, expected: bool) -> None:
    assert CppTestBlockProfile().handles_file(rel_path) is expected


# ---------------------------------------------------------------------------
# parse_test_blocks — the 4 gtest fixture bodies + skip variants
# ---------------------------------------------------------------------------


def _only_block(text: str):
    blocks = CppTestBlockProfile().parse_test_blocks(text)
    assert len(blocks) == 1, f"expected exactly one block, got {len(blocks)}: {blocks}"
    return blocks[0]


def test_parse_gtest_fake_no_assertion() -> None:
    block = _only_block("TEST(MySuite, Foo) { add(2, 3); }\n")
    assert block.is_executable is True
    assert block.has_assertion is False  # a bare SUT call is not a primitive
    assert block.label == "MySuite.Foo"


def test_parse_gtest_fake_constant_only_expect_true() -> None:
    block = _only_block("TEST(MySuite, Foo) { EXPECT_TRUE(true); }\n")
    assert block.is_executable is True
    assert block.has_assertion is True  # EXPECT_TRUE IS a primitive...
    # ...but a constant one (decided by resolve_direct_assertion_evidence below).
    assert (
        CppTestBlockProfile().resolve_direct_assertion_evidence(block).reason
        == "constant_direct"
    )


def test_parse_gtest_fake_constant_only_expect_eq() -> None:
    block = _only_block("TEST(MySuite, Foo) { EXPECT_EQ(1, 1); }\n")
    assert block.has_assertion is True
    assert (
        CppTestBlockProfile().resolve_direct_assertion_evidence(block).reason
        == "constant_direct"
    )


def test_parse_gtest_disabled_prefix_not_executable() -> None:
    # gtest disables a test by a DISABLED_ prefix on the test NAME.
    block = _only_block("TEST(MySuite, DISABLED_Foo) { EXPECT_EQ(5, add(2, 3)); }\n")
    assert block.is_executable is False
    assert block.label == "MySuite.DISABLED_Foo"


def test_parse_gtest_disabled_suite_prefix_not_executable() -> None:
    # gtest also disables an entire SUITE by a DISABLED_ prefix on the suite name.
    block = _only_block("TEST(DISABLED_MySuite, Foo) { EXPECT_EQ(5, add(2, 3)); }\n")
    assert block.is_executable is False


def test_parse_gtest_body_gtest_skip_not_executable() -> None:
    # A body that calls GTEST_SKIP() is not executable even without DISABLED_.
    block = _only_block("TEST(MySuite, Foo) { GTEST_SKIP(); EXPECT_EQ(5, add(2, 3)); }\n")
    assert block.is_executable is False


def test_parse_gtest_real_covering_executable_with_assertion() -> None:
    block = _only_block("TEST(MySuite, Foo) { EXPECT_EQ(5, add(2, 3)); }\n")
    assert block.is_executable is True
    assert block.has_assertion is True
    assert (
        CppTestBlockProfile().resolve_direct_assertion_evidence(block).reason
        == "direct"
    )


def test_parse_test_f_and_test_p_macros() -> None:
    # TEST_F (fixture) and TEST_P (parameterized) share the gtest body grammar.
    text = (
        "TEST_F(MyFixture, Foo) { EXPECT_EQ(5, add(2, 3)); }\n"
        "TEST_P(MyParam, Bar) { EXPECT_TRUE(check(x)); }\n"
    )
    blocks = CppTestBlockProfile().parse_test_blocks(text)
    assert len(blocks) == 2
    labels = {b.label for b in blocks}
    assert labels == {"MyFixture.Foo", "MyParam.Bar"}
    assert all(b.has_assertion and b.is_executable for b in blocks)


def test_parse_multiline_body_brace_matched() -> None:
    text = (
        "TEST(MySuite, Foo) {\n"
        "    int got = add(2, 3);\n"
        "    EXPECT_EQ(5, got);\n"
        "}\n"
    )
    block = _only_block(text)
    assert block.is_executable is True
    assert block.has_assertion is True
    assert block.start_line == 1
    assert block.end_line == 4
    # references `got` (a local) -> direct
    assert (
        CppTestBlockProfile().resolve_direct_assertion_evidence(block).reason
        == "direct"
    )


def test_assertion_in_comment_is_not_primitive() -> None:
    # A fake assertion written in a COMMENT must NOT count (false-GREEN guard).
    text = "TEST(MySuite, Foo) {\n    // EXPECT_EQ(5, add(2, 3));\n    add(2, 3);\n}\n"
    block = _only_block(text)
    assert block.has_assertion is False


def test_succeed_is_not_a_primitive_assertion() -> None:
    # SUCCEED() proves nothing — it is the C++ analogue of a constant assertion
    # and is deliberately NOT in the primitive macro set.
    block = _only_block("TEST(MySuite, Foo) { SUCCEED(); }\n")
    assert block.has_assertion is False


# ---------------------------------------------------------------------------
# resolve_direct_assertion_evidence — constant vs real
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected_reason",
    [
        ("EXPECT_EQ(1, 1);", "constant_direct"),
        ("EXPECT_TRUE(true);", "constant_direct"),
        ("EXPECT_FALSE(false);", "constant_direct"),
        ("ASSERT_EQ(0, 0);", "constant_direct"),
        ("EXPECT_EQ(5, add(2, 3));", "direct"),       # references add
        ("EXPECT_TRUE(is_valid(x));", "direct"),       # references is_valid, x
        ("ASSERT_EQ(expected, actual);", "direct"),    # references locals
        ("EXPECT_EQ(got, 5);", "direct"),              # references got
    ],
)
def test_resolve_direct_assertion_evidence(body: str, expected_reason: str) -> None:
    block = _only_block(f"TEST(MySuite, Foo) {{ {body} }}\n")
    assert block.has_assertion is True
    evidence = CppTestBlockProfile().resolve_direct_assertion_evidence(block)
    assert evidence.reason == expected_reason
    assert evidence.ok is (expected_reason == "direct")


# ---------------------------------------------------------------------------
# resolve_assertion_evidence — fail-closed helper path (no spurious ok=True)
# ---------------------------------------------------------------------------


def test_resolve_assertion_evidence_no_assertion() -> None:
    # A body with neither a primitive nor an assertion-like helper call.
    block = _only_block("TEST(MySuite, Foo) { add(2, 3); }\n")
    evidence = CppTestBlockProfile().resolve_assertion_evidence(
        block, importer_text="", importer_rel="tests/x_test.cpp", project_root=None
    )
    assert evidence.ok is False
    assert evidence.reason == "no_assertion"


def test_resolve_assertion_evidence_unresolved_helper() -> None:
    # An assertion-like bare helper call we cannot resolve is fail-CLOSED.
    block = _only_block("TEST(MySuite, Foo) { verifyResult(r); }\n")
    evidence = CppTestBlockProfile().resolve_assertion_evidence(
        block, importer_text="", importer_rel="tests/x_test.cpp", project_root=None
    )
    assert evidence.ok is False
    assert evidence.reason == "unresolved_helper"


# ---------------------------------------------------------------------------
# Catch2 smoke
# ---------------------------------------------------------------------------


def test_parse_catch2_test_case_with_require() -> None:
    block = _only_block('TEST_CASE("x", "[t]") { REQUIRE(add(2, 3) == 5); }\n')
    assert block.is_executable is True
    assert block.has_assertion is True  # REQUIRE is a primitive
    assert block.label == "x"
    # REQUIRE(add(2,3) == 5) references add -> direct
    assert (
        CppTestBlockProfile().resolve_direct_assertion_evidence(block).reason
        == "direct"
    )


def test_parse_catch2_constant_require_is_constant_direct() -> None:
    block = _only_block('TEST_CASE("x", "[t]") { REQUIRE(true); }\n')
    assert block.has_assertion is True
    assert (
        CppTestBlockProfile().resolve_direct_assertion_evidence(block).reason
        == "constant_direct"
    )


def test_parse_catch2_hidden_tag_not_executable() -> None:
    # A leading-`.` tag hides a Catch2 case from the default run.
    block = _only_block('TEST_CASE("x", "[.]") { REQUIRE(add(2, 3) == 5); }\n')
    assert block.is_executable is False


# ---------------------------------------------------------------------------
# Unparseable / robustness — never raise, return []
# ---------------------------------------------------------------------------


def test_parse_unparseable_returns_empty_never_raises() -> None:
    p = CppTestBlockProfile()
    assert p.parse_test_blocks("") == []
    assert p.parse_test_blocks("int main() { return 0; }\n") == []
    # A truncated test (no closing brace) must not raise.
    blocks = p.parse_test_blocks("TEST(MySuite, Foo) { EXPECT_EQ(5, add(2, 3));")
    assert isinstance(blocks, list)


# ---------------------------------------------------------------------------
# END-TO-END through build_authenticity_report (the conformance shape)
# ---------------------------------------------------------------------------


class _Stub:
    """Minimal LayoutProfile-like object exposing the C++ block adapter."""

    language = "cpp"

    def test_block_profile(self):
        return CppTestBlockProfile()


_FIXTURES = {
    "fake_no_assertion": f"#include <gtest/gtest.h>\n{_MARKER}\nTEST(MySuite, Foo) {{ add(2, 3); }}\n",
    "fake_constant_only": f"#include <gtest/gtest.h>\n{_MARKER}\nTEST(MySuite, Foo) {{ EXPECT_TRUE(true); }}\n",
    "fake_skipped": f"#include <gtest/gtest.h>\n{_MARKER}\nTEST(MySuite, DISABLED_Foo) {{ EXPECT_EQ(5, add(2, 3)); }}\n",
    "real_covering": f"#include <gtest/gtest.h>\n{_MARKER}\nTEST(MySuite, Foo) {{ EXPECT_EQ(5, add(2, 3)); }}\n",
}


def _run_case(tmp_path, case_name: str) -> bool:
    root = tmp_path / f"cpp_{case_name}"
    (root / "docs" / "test").mkdir(parents=True)
    (root / "docs" / "test" / "test_strategy.md").write_text(
        "| VB | D |\n| --- | --- |\n| VB-01 | demo |\n"
    )
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "x_test.cpp").write_text(_FIXTURES[case_name])
    report = build_authenticity_report(
        root, config={"scan": {"test_dirs": ["tests/"]}}, profile=_Stub()
    )
    return report.passed


@pytest.mark.parametrize("case_name", sorted(_FIXTURES))
def test_end_to_end_authenticity_verdicts(tmp_path, case_name: str) -> None:
    """3 fakes rejected, the real covering test credited — the cardinal
    anti-false-green contract, exercised through the full gate."""
    passed = _run_case(tmp_path, case_name)
    expected = case_name.startswith("real_")
    assert passed is expected, (
        f"{case_name}: build_authenticity_report passed={passed}, expected {expected}"
    )
