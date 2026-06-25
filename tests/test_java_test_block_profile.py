"""Focused unit + end-to-end tests for :class:`JavaTestBlockProfile`.

The cross-language CONFORMANCE contract (``tests/test_profile_conformance.py``)
proves the cardinal *false-green escape == 0* property for every registered
profile once the parent wires ``java-junit-semantics`` ->
:class:`JavaTestBlockProfile` into ``project_types.py``'s adapter-id table. This
module is the Java profile's OWN white-box test: it exercises the parser's
``handles_file`` / ``parse_test_blocks`` / ``resolve_direct_assertion_evidence``
directly, and runs the FULL authenticity gate (``build_authenticity_report``)
against the four conformance fixtures via a profile STUB that returns the parser
directly -- so the gate is exercised with this parser REGARDLESS of whether the
parent has wired the registry yet (until wired, ``LayoutProfile.test_block_
profile()`` returns ``None`` for Java and the gate would silently degrade).

The four fixtures mirror the conformance cases verbatim (a ``@Test`` method whose
body either has no assertion, a constant-only assertion, is ``@Disabled``, or is
a genuine covering assertion). The marker sits on the line immediately ABOVE the
``@Test`` annotation, as the gate's block-ized attachment requires.
"""
from __future__ import annotations

import pathlib

import pytest

from codd.vb_marker_authenticity import (
    JavaTestBlockProfile,
    TestBlock,
    build_authenticity_report,
)

_MARKER = "// codd: covers vb=VB-01"


# ---------------------------------------------------------------------------
# The four conformance fixtures (complete .java files), keyed exactly as the
# parent will key them under CONFORMANCE_FIXTURES["java"]["cases"]. A ``real_*``
# case MUST be credited; every other case is a FAKE that MUST be rejected.
# ---------------------------------------------------------------------------
FIXTURE_FILENAME = "XTest.java"

FIXTURES: dict[str, str] = {
    "fake_no_assertion": (
        "import org.junit.jupiter.api.Test;\n\n"
        "class XTest {\n"
        f"    {_MARKER}\n"
        "    @Test\n"
        "    void x() {\n"
        "        add(2, 3);\n"
        "    }\n"
        "}\n"
    ),
    "fake_constant_only": (
        "import org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class XTest {\n"
        f"    {_MARKER}\n"
        "    @Test\n"
        "    void x() {\n"
        "        assertEquals(1, 1);\n"
        "    }\n"
        "}\n"
    ),
    "fake_skipped": (
        "import org.junit.jupiter.api.Test;\n"
        "import org.junit.jupiter.api.Disabled;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class XTest {\n"
        f"    {_MARKER}\n"
        "    @Disabled\n"
        "    @Test\n"
        "    void x() {\n"
        "        assertEquals(5, add(2, 3));\n"
        "    }\n"
        "}\n"
    ),
    "real_covering": (
        "import org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class XTest {\n"
        f"    {_MARKER}\n"
        "    @Test\n"
        "    void x() {\n"
        "        assertEquals(5, add(2, 3));\n"
        "    }\n"
        "}\n"
    ),
}

#: (is_executable, has_assertion) the parser MUST report for each fixture's block.
_EXPECTED_BLOCK_FACTS: dict[str, tuple[bool, bool]] = {
    "fake_no_assertion": (True, False),
    "fake_constant_only": (True, True),
    "fake_skipped": (False, True),
    "real_covering": (True, True),
}


# ---------------------------------------------------------------------------
# handles_file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path, expected",
    [
        ("tests/XTest.java", True),  # conformance fixture lives here
        ("src/test/java/com/x/FooTest.java", True),  # Maven test source root
        ("src/test/java/com/x/FooTests.java", True),
        ("src/test/java/com/x/FooIT.java", True),
        ("tests/CalculatorTests.java", True),  # *Tests basename outside maven root
        ("tests/PaymentIT.java", True),  # *IT basename
        ("src/main/java/com/x/Main.java", False),  # production source
        ("Main.java", False),  # non-test basename, no test dir
        ("tests/Helpers.java", False),  # support file, not a *Test class
        ("tests/x.test.ts", False),  # not java at all
    ],
)
def test_handles_file(rel_path: str, expected: bool):
    assert JavaTestBlockProfile().handles_file(rel_path) is expected


# ---------------------------------------------------------------------------
# parse_test_blocks -- one leaf block per @Test method, with skip/assertion facts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_name", sorted(FIXTURES))
def test_parse_test_blocks_facts(case_name: str):
    blocks = JavaTestBlockProfile().parse_test_blocks(FIXTURES[case_name])
    assert len(blocks) == 1, f"{case_name}: expected exactly one @Test block"
    block = blocks[0]
    assert block.label == "x"
    exp_exec, exp_assert = _EXPECTED_BLOCK_FACTS[case_name]
    assert block.is_executable is exp_exec, f"{case_name}: is_executable"
    assert block.has_assertion is exp_assert, f"{case_name}: has_assertion"


def test_parse_ignores_non_test_method():
    """A plain (non-``@Test``) method is NOT a coverage target."""
    text = (
        "import org.junit.jupiter.api.Test;\n\n"
        "class XTest {\n"
        "    void helper() { add(1, 2); }\n"
        "    @Test\n"
        "    void realTest() { add(3, 4); }\n"
        "}\n"
    )
    blocks = JavaTestBlockProfile().parse_test_blocks(text)
    labels = sorted(b.label for b in blocks)
    assert labels == ["realTest"], "only the @Test method is a block"


def test_parse_multiple_test_methods():
    """Several ``@Test`` methods in one class -> several leaf blocks."""
    text = (
        "import org.junit.jupiter.api.Test;\n"
        "import org.junit.jupiter.api.Disabled;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class MultiTest {\n"
        "    @Test\n"
        "    void a() { assertEquals(2, add(1, 1)); }\n"
        "    @Disabled\n"
        "    @Test\n"
        "    void b() { assertEquals(3, add(1, 2)); }\n"
        "}\n"
    )
    blocks = {b.label: b for b in JavaTestBlockProfile().parse_test_blocks(text)}
    assert set(blocks) == {"a", "b"}
    assert blocks["a"].is_executable is True
    assert blocks["a"].has_assertion is True
    assert blocks["b"].is_executable is False  # @Disabled


def test_assertion_in_comment_does_not_count():
    """A primitive assertion written in a COMMENT must not set has_assertion
    (the false-GREEN guard via comment stripping)."""
    text = (
        "import org.junit.jupiter.api.Test;\n\n"
        "class XTest {\n"
        "    @Test\n"
        "    void x() {\n"
        "        // assertEquals(5, add(2, 3));\n"
        "        add(2, 3);\n"
        "    }\n"
        "}\n"
    )
    block = JavaTestBlockProfile().parse_test_blocks(text)[0]
    assert block.has_assertion is False


def test_parse_never_raises_on_garbage():
    """A best-effort parse of malformed input returns ``[]`` (degrade), never raises."""
    for junk in ["", "not java at all {{{", "@Test void broken( {", "class C {"]:
        assert JavaTestBlockProfile().parse_test_blocks(junk) == []


def test_assumptions_abort_marks_not_executable():
    """A body that unconditionally aborts (``Assumptions.abort``) is not executable."""
    text = (
        "import org.junit.jupiter.api.Test;\n"
        "import org.junit.jupiter.api.Assumptions;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class XTest {\n"
        "    @Test\n"
        "    void x() {\n"
        "        Assumptions.abort(\"not ready\");\n"
        "        assertEquals(5, add(2, 3));\n"
        "    }\n"
        "}\n"
    )
    block = JavaTestBlockProfile().parse_test_blocks(text)[0]
    assert block.is_executable is False


# ---------------------------------------------------------------------------
# resolve_direct_assertion_evidence -- constant-only vs real
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected_reason, expected_ok",
    [
        ("assertEquals(1, 1);", "constant_direct", False),
        ("assertTrue(true);", "constant_direct", False),
        ("assertFalse(false);", "constant_direct", False),
        ("assertNull(null);", "constant_direct", False),
        ("assertEquals(5, add(2, 3));", "direct", True),  # references add
        ("assertTrue(result.isValid());", "direct", True),  # references result
        ("assertThat(actual).isEqualTo(7);", "direct", True),  # AssertJ, references actual
    ],
)
def test_resolve_direct_assertion_evidence(body, expected_reason, expected_ok):
    block = TestBlock(
        start_line=1, end_line=3, is_executable=True, has_assertion=True,
        label="x", body_text=body,
    )
    ev = JavaTestBlockProfile().resolve_direct_assertion_evidence(block)
    assert ev.reason == expected_reason
    assert ev.ok is expected_ok


def test_resolve_assertion_evidence_is_fail_closed():
    """The helper-resolution path NEVER credits (1-hop Java resolution unbuilt)."""
    profile = JavaTestBlockProfile()
    # bare SUT call, no assertion-like helper -> no_assertion (not ok)
    blk_plain = TestBlock(1, 3, True, False, "x", "add(2, 3);")
    ev_plain = profile.resolve_assertion_evidence(
        blk_plain, importer_text="", importer_rel="tests/XTest.java", project_root=pathlib.Path(".")
    )
    assert ev_plain.ok is False
    assert ev_plain.reason == "no_assertion"

    # assertion-LIKE helper call (cannot resolve) -> unresolved_helper (not ok)
    blk_helper = TestBlock(1, 3, True, False, "x", "verifyResult(actual);")
    ev_helper = profile.resolve_assertion_evidence(
        blk_helper, importer_text="", importer_rel="tests/XTest.java", project_root=pathlib.Path(".")
    )
    assert ev_helper.ok is False
    assert ev_helper.reason == "unresolved_helper"


# ---------------------------------------------------------------------------
# END-TO-END: the full authenticity gate, mirroring the conformance harness.
#
# We bypass the parent's not-yet-wired registry by passing a profile STUB whose
# ``test_block_profile()`` returns ``JavaTestBlockProfile()`` directly. This
# exercises the entire gate (marker scan -> block parse -> attachment ->
# assertion evidence) with THIS parser regardless of project_types wiring.
# ---------------------------------------------------------------------------


class _Stub:
    """Minimal LayoutProfile-like object the gate can resolve an adapter from."""

    language = "java"

    def test_block_profile(self):
        return JavaTestBlockProfile()


def _run_gate(tmp_path: pathlib.Path, case_name: str) -> bool:
    root = tmp_path / f"java_{case_name}"
    (root / "docs" / "test").mkdir(parents=True)
    (root / "docs" / "test" / "test_strategy.md").write_text(
        "| VB | D |\n| --- | --- |\n| VB-01 | demo |\n"
    )
    (root / "tests").mkdir(parents=True)
    (root / "tests" / FIXTURE_FILENAME).write_text(FIXTURES[case_name])
    report = build_authenticity_report(
        root, config={"scan": {"test_dirs": ["tests/"]}}, profile=_Stub()
    )
    return report.passed


@pytest.mark.parametrize(
    "case_name, expected_passed",
    [
        ("fake_no_assertion", False),
        ("fake_constant_only", False),
        ("fake_skipped", False),
        ("real_covering", True),
    ],
)
def test_end_to_end_gate_verdicts(tmp_path, case_name, expected_passed):
    """Cardinal anti-false-green: 3 fakes rejected, the real covering test credited."""
    assert _run_gate(tmp_path, case_name) is expected_passed


def test_end_to_end_no_false_green_escape(tmp_path):
    """Aggregate guard mirroring the conformance assertion exactly."""
    false_green_escapes = []
    false_reds = []
    for case_name in FIXTURES:
        passed = _run_gate(tmp_path, case_name)
        expected = case_name.startswith("real_")
        if passed and not expected:
            false_green_escapes.append(case_name)
        elif not passed and expected:
            false_reds.append(case_name)
    assert not false_green_escapes, f"FALSE-GREEN ESCAPE: {false_green_escapes}"
    assert not false_reds, f"false-RED: {false_reds}"
