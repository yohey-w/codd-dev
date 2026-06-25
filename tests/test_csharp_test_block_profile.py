"""Focused unit + end-to-end tests for :class:`CSharpTestBlockProfile`.

The cross-language CONFORMANCE contract (``tests/test_profile_conformance.py``)
proves the cardinal *false-green escape == 0* property for every registered
profile once the parent wires ``csharp-test-semantics`` ->
:class:`CSharpTestBlockProfile` into ``project_types.py``'s adapter-id table. This
module is the C# profile's OWN white-box test: it exercises the parser's
``handles_file`` / ``parse_test_blocks`` / ``resolve_direct_assertion_evidence``
directly, and runs the FULL authenticity gate (``build_authenticity_report``)
against the four conformance fixtures via a profile STUB that returns the parser
directly -- so the gate is exercised with this parser REGARDLESS of whether the
parent has wired the registry yet (until wired, ``LayoutProfile.test_block_
profile()`` returns ``None`` for C# and the gate would silently degrade).

The four fixtures mirror the conformance cases verbatim (a ``[Fact]`` method whose
body either has no assertion, a constant-only assertion, is ``[Fact(Skip="wip")]``,
or is a genuine covering assertion). The marker sits on the line immediately ABOVE
the ``[Fact]`` attribute, as the gate's block-ized attachment requires.
"""
from __future__ import annotations

import pathlib

import pytest

from codd.vb_marker_authenticity import (
    CSharpTestBlockProfile,
    TestBlock,
    build_authenticity_report,
)

_MARKER = "// codd: covers vb=VB-01"


# ---------------------------------------------------------------------------
# The four conformance fixtures (complete .cs files), keyed exactly as the
# parent will key them under CONFORMANCE_FIXTURES["csharp"]["cases"]. A ``real_*``
# case MUST be credited; every other case is a FAKE that MUST be rejected. The
# methods are wrapped in a ``class XTests { ... }`` with ``using Xunit;`` (C# tests
# live in a class), and the marker sits on the line immediately ABOVE the
# ``[Fact]`` attribute -- the placement the gate's block-ized attachment requires.
# ---------------------------------------------------------------------------
FIXTURE_FILENAME = "XTests.cs"

FIXTURES: dict[str, str] = {
    "fake_no_assertion": (
        "using Xunit;\n\n"
        "public class XTests {\n"
        f"    {_MARKER}\n"
        "    [Fact]\n"
        "    public void X() {\n"
        "        Add(2, 3);\n"
        "    }\n"
        "}\n"
    ),
    "fake_constant_only": (
        "using Xunit;\n\n"
        "public class XTests {\n"
        f"    {_MARKER}\n"
        "    [Fact]\n"
        "    public void X() {\n"
        "        Assert.True(true);\n"
        "    }\n"
        "}\n"
    ),
    "fake_skipped": (
        "using Xunit;\n\n"
        "public class XTests {\n"
        f"    {_MARKER}\n"
        '    [Fact(Skip="wip")]\n'
        "    public void X() {\n"
        "        Assert.Equal(5, Add(2, 3));\n"
        "    }\n"
        "}\n"
    ),
    "real_covering": (
        "using Xunit;\n\n"
        "public class XTests {\n"
        f"    {_MARKER}\n"
        "    [Fact]\n"
        "    public void X() {\n"
        "        Assert.Equal(5, Add(2, 3));\n"
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
        ("tests/XTests.cs", True),  # conformance fixture lives here
        ("tests/CalculatorTests.cs", True),  # *Tests basename under tests/
        ("tests/PaymentTest.cs", True),  # *Test basename under tests/
        ("src/App.Tests/FooTests.cs", True),  # .Tests project dir
        ("MyProject.Tests/CalcTest.cs", True),  # .Tests dir at root
        ("CalculatorTests.cs", True),  # *Tests basename, no dir
        ("FooTest.cs", True),  # *Test basename, no dir
        ("src/Calculator.cs", False),  # production source
        ("App.cs", False),  # non-test basename, no test dir
        ("tests/Helpers.cs", True),  # under tests/ dir -> handled (gate degrades if unparsable)
        ("tests/x.test.ts", False),  # not C# at all
    ],
)
def test_handles_file(rel_path: str, expected: bool):
    assert CSharpTestBlockProfile().handles_file(rel_path) is expected


# ---------------------------------------------------------------------------
# parse_test_blocks -- one leaf block per [Fact] method, with skip/assertion facts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_name", sorted(FIXTURES))
def test_parse_test_blocks_facts(case_name: str):
    blocks = CSharpTestBlockProfile().parse_test_blocks(FIXTURES[case_name])
    assert len(blocks) == 1, f"{case_name}: expected exactly one [Fact] block"
    block = blocks[0]
    assert block.label == "X"
    exp_exec, exp_assert = _EXPECTED_BLOCK_FACTS[case_name]
    assert block.is_executable is exp_exec, f"{case_name}: is_executable"
    assert block.has_assertion is exp_assert, f"{case_name}: has_assertion"


def test_parse_ignores_non_test_method():
    """A plain (non-test-attributed) method is NOT a coverage target."""
    text = (
        "using Xunit;\n\n"
        "public class XTests {\n"
        "    private void Helper() { Add(1, 2); }\n"
        "    [Fact]\n"
        "    public void RealTest() { Assert.Equal(7, Add(3, 4)); }\n"
        "}\n"
    )
    blocks = CSharpTestBlockProfile().parse_test_blocks(text)
    labels = sorted(b.label for b in blocks)
    assert labels == ["RealTest"], "only the [Fact] method is a block"


def test_parse_multiple_test_methods():
    """Several attributed methods in one class -> several leaf blocks."""
    text = (
        "using Xunit;\n\n"
        "public class MultiTests {\n"
        "    [Fact]\n"
        "    public void A() { Assert.Equal(2, Add(1, 1)); }\n"
        '    [Fact(Skip="later")]\n'
        "    public void B() { Assert.Equal(3, Add(1, 2)); }\n"
        "}\n"
    )
    blocks = {b.label: b for b in CSharpTestBlockProfile().parse_test_blocks(text)}
    assert set(blocks) == {"A", "B"}
    assert blocks["A"].is_executable is True
    assert blocks["A"].has_assertion is True
    assert blocks["B"].is_executable is False  # Skip= argument


def test_parse_recognizes_nunit_and_mstest_attributes():
    """NUnit ``[Test]`` and MSTest ``[TestMethod]`` are also test blocks; NUnit
    ``[Ignore]`` and MSTest ``[Ignore]`` mark a method skipped."""
    text = (
        "using NUnit.Framework;\n\n"
        "public class MixTests {\n"
        "    [Test]\n"
        "    public void NunitOk() { Assert.AreEqual(2, Add(1, 1)); }\n"
        '    [Test]\n'
        '    [Ignore("flaky")]\n'
        "    public void NunitSkipped() { Assert.AreEqual(3, Add(1, 2)); }\n"
        "    [TestMethod]\n"
        "    public void MstestOk() { Assert.IsTrue(IsReady()); }\n"
        "}\n"
    )
    blocks = {b.label: b for b in CSharpTestBlockProfile().parse_test_blocks(text)}
    assert set(blocks) == {"NunitOk", "NunitSkipped", "MstestOk"}
    assert blocks["NunitOk"].is_executable is True
    assert blocks["NunitOk"].has_assertion is True
    assert blocks["NunitSkipped"].is_executable is False  # [Ignore]
    assert blocks["MstestOk"].has_assertion is True


def test_parse_tolerates_extra_attributes_between_marker_and_fact():
    """A ``[Trait(...)]`` / ``[DisplayName(...)]`` stacked with ``[Fact]`` does not
    hide the test block, and the block still starts at the FIRST test attribute."""
    text = (
        "using Xunit;\n\n"
        "public class XTests {\n"
        '    [Trait("cat", "unit")]\n'
        "    [Fact]\n"
        '    [Trait("speed", "fast")]\n'
        "    public void X() { Assert.Equal(5, Add(2, 3)); }\n"
        "}\n"
    )
    blocks = CSharpTestBlockProfile().parse_test_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].label == "X"
    assert blocks[0].is_executable is True
    assert blocks[0].has_assertion is True


def test_assertion_in_comment_does_not_count():
    """A primitive assertion written in a COMMENT must not set has_assertion
    (the false-GREEN guard via comment stripping)."""
    text = (
        "using Xunit;\n\n"
        "public class XTests {\n"
        "    [Fact]\n"
        "    public void X() {\n"
        "        // Assert.Equal(5, Add(2, 3));\n"
        "        Add(2, 3);\n"
        "    }\n"
        "}\n"
    )
    block = CSharpTestBlockProfile().parse_test_blocks(text)[0]
    assert block.has_assertion is False


def test_skip_in_comment_does_not_mark_skipped():
    """A ``Skip=`` written in a COMMENT must not mark a real test skipped
    (the opposite hazard -- a false-RED)."""
    text = (
        "using Xunit;\n\n"
        "public class XTests {\n"
        '    // [Fact(Skip="not really")]\n'
        "    [Fact]\n"
        "    public void X() { Assert.Equal(5, Add(2, 3)); }\n"
        "}\n"
    )
    block = CSharpTestBlockProfile().parse_test_blocks(text)[0]
    assert block.is_executable is True


def test_parse_never_raises_on_garbage():
    """A best-effort parse of malformed input returns ``[]`` (degrade), never raises."""
    for junk in ["", "not c# at all {{{", "[Fact] public void broken( {", "class C {"]:
        assert CSharpTestBlockProfile().parse_test_blocks(junk) == []


# ---------------------------------------------------------------------------
# resolve_direct_assertion_evidence -- constant-only vs real
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, expected_reason, expected_ok",
    [
        ("Assert.Equal(1, 1);", "constant_direct", False),
        ("Assert.True(true);", "constant_direct", False),
        ("Assert.False(false);", "constant_direct", False),
        ("Assert.Null(null);", "constant_direct", False),
        ("Assert.Equal(5, Add(2, 3));", "direct", True),  # references Add
        ("Assert.True(result.IsValid());", "direct", True),  # references result
        ("Assert.AreEqual(expected, actual);", "direct", True),  # NUnit, references vars
    ],
)
def test_resolve_direct_assertion_evidence(body, expected_reason, expected_ok):
    block = TestBlock(
        start_line=1, end_line=3, is_executable=True, has_assertion=True,
        label="X", body_text=body,
    )
    ev = CSharpTestBlockProfile().resolve_direct_assertion_evidence(block)
    assert ev.reason == expected_reason
    assert ev.ok is expected_ok


def test_resolve_assertion_evidence_is_fail_closed():
    """The helper-resolution path NEVER credits (1-hop C# resolution unbuilt)."""
    profile = CSharpTestBlockProfile()
    # bare SUT call, no assertion-like helper -> no_assertion (not ok)
    blk_plain = TestBlock(1, 3, True, False, "X", "Add(2, 3);")
    ev_plain = profile.resolve_assertion_evidence(
        blk_plain, importer_text="", importer_rel="tests/XTests.cs", project_root=pathlib.Path(".")
    )
    assert ev_plain.ok is False
    assert ev_plain.reason == "no_assertion"

    # assertion-LIKE helper call (cannot resolve) -> unresolved_helper (not ok)
    blk_helper = TestBlock(1, 3, True, False, "X", "VerifyResult(actual);")
    ev_helper = profile.resolve_assertion_evidence(
        blk_helper, importer_text="", importer_rel="tests/XTests.cs", project_root=pathlib.Path(".")
    )
    assert ev_helper.ok is False
    assert ev_helper.reason == "unresolved_helper"


# ---------------------------------------------------------------------------
# END-TO-END: the full authenticity gate, mirroring the conformance harness.
#
# We bypass the parent's not-yet-wired registry by passing a profile STUB whose
# ``test_block_profile()`` returns ``CSharpTestBlockProfile()`` directly. This
# exercises the entire gate (marker scan -> block parse -> attachment ->
# assertion evidence) with THIS parser regardless of project_types wiring.
# ---------------------------------------------------------------------------


class _Stub:
    """Minimal LayoutProfile-like object the gate can resolve an adapter from."""

    language = "csharp"

    def test_block_profile(self):
        return CSharpTestBlockProfile()


def _run_gate(tmp_path: pathlib.Path, case_name: str) -> bool:
    root = tmp_path / f"csharp_{case_name}"
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
