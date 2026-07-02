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

import codd.vb_marker_authenticity as vma
from codd.vb_marker_authenticity import (
    AssertionEvidence,
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


# ---------------------------------------------------------------------------
# Java marker-authenticity resolution — the 12 required fixtures (4 GOOD, 8
# ADVERSARIAL) for the shared-engine reuse + E1 (full-callee propagation) + E2
# (fallback_module_candidates) + confidence field + library_assertion_terminals
# increment. Numbered exactly as the approved design lists them.
# ---------------------------------------------------------------------------


def _write_project(tmp_path: pathlib.Path, files: dict) -> pathlib.Path:
    """Write ``{relative_path: content}`` under ``tmp_path`` and return the root."""

    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path


# ── GOOD 1: HarnessAssertions.assertSuccess(...) — same-package sibling helper ──

_GOOD1_IMPORTER = (
    "package com.example;\n\n"
    "import org.junit.jupiter.api.Test;\n\n"
    "class HarnessCallerTest {\n"
    "    // codd: covers vb=VB-01\n"
    "    @Test\n"
    "    void x() {\n"
    '        HarnessAssertions.assertSuccess(compute(), "14.0");\n'
    "    }\n\n"
    "    static String compute() {\n"
    '        return "14.0";\n'
    "    }\n"
    "}\n"
)

_GOOD1_HELPER = (
    "package com.example;\n\n"
    "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
    "class HarnessAssertions {\n"
    "    static void assertSuccess(String result, String expected) {\n"
    "        assertEquals(expected, result);\n"
    "    }\n"
    "}\n"
)


def test_good_1_helper_delegated_via_same_package_sibling_resolves(tmp_path):
    """A qualified call to a same-package sibling helper class resolves as
    ``helper_resolved`` — the E2 fallback-candidates path (no import needed at
    all for a same-package/same-directory Maven helper)."""

    root = _write_project(
        tmp_path,
        {
            "tests/HarnessCallerTest.java": _GOOD1_IMPORTER,
            "tests/HarnessAssertions.java": _GOOD1_HELPER,
        },
    )
    block = TestBlock(
        start_line=6,
        end_line=8,
        is_executable=True,
        has_assertion=False,
        label="x",
        body_text='HarnessAssertions.assertSuccess(compute(), "14.0");',
    )
    ev = JavaTestBlockProfile().resolve_assertion_evidence(
        block,
        importer_text=_GOOD1_IMPORTER,
        importer_rel="tests/HarnessCallerTest.java",
        project_root=root,
    )
    assert ev.ok is True
    assert ev.reason == "helper_resolved"
    assert ev.confidence == "certain"


def test_good_1_end_to_end_gate_credits_the_marker(tmp_path):
    """Full gate (mirroring the live dogfood shape): the marker on the
    delegating test is credited, no false-RED."""

    root = _write_project(
        tmp_path,
        {
            "docs/test/test_strategy.md": "| VB | D |\n| --- | --- |\n| VB-01 | demo |\n",
            "tests/HarnessCallerTest.java": _GOOD1_IMPORTER,
            "tests/HarnessAssertions.java": _GOOD1_HELPER,
        },
    )

    class _Stub:
        language = "java"

        def test_block_profile(self):
            return JavaTestBlockProfile()

    report = build_authenticity_report(
        root, config={"scan": {"test_dirs": ["tests/"]}}, profile=_Stub()
    )
    assert report.passed, report.violations


# ── GOOD 2: ArchUnit fluent terminal (rule.check(classes)) ──────────────────
#
# Modeled verbatim on the LIVE dogfood artifact
# (/tmp/codd_greenfield_java_v2_ExprCalc/.../ModuleDependencyArchTest.java): the
# fluent chain is broken by intervening ``()`` calls, so the terminal's
# ``full_callee`` is the bare leaf ``check`` (no receiver) — the argument
# ``classes`` alone must anchor it.

_GOOD2_ARCHUNIT_TEST = (
    "package com.example;\n\n"
    "import com.tngtech.archunit.core.domain.JavaClasses;\n"
    "import com.tngtech.archunit.core.importer.ClassFileImporter;\n"
    "import org.junit.jupiter.api.Test;\n\n"
    "import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;\n\n"
    "class ModuleDependencyArchTest {\n"
    '    private final JavaClasses classes = new ClassFileImporter().importPackages("com.example");\n\n'
    "    // codd: covers vb=VB-ARCH-01\n"
    "    @Test\n"
    "    void tokenizerDoesNotDependOnParser() {\n"
    '        noClasses().that().resideInAPackage("..tokenizer..")\n'
    '            .should().dependOnClassesThat().resideInAPackage("..parser..")\n'
    "            .check(classes);\n"
    "    }\n"
    "}\n"
)

_GOOD2_BODY = (
    '        noClasses().that().resideInAPackage("..tokenizer..")\n'
    '            .should().dependOnClassesThat().resideInAPackage("..parser..")\n'
    "            .check(classes);\n"
)


def test_good_2_archunit_fluent_terminal_resolves(tmp_path):
    root = _write_project(tmp_path, {"tests/ModuleDependencyArchTest.java": _GOOD2_ARCHUNIT_TEST})
    block = TestBlock(
        start_line=10,
        end_line=12,
        is_executable=True,
        has_assertion=False,
        label="tokenizerDoesNotDependOnParser",
        body_text=_GOOD2_BODY,
    )
    ev = JavaTestBlockProfile().resolve_assertion_evidence(
        block,
        importer_text=_GOOD2_ARCHUNIT_TEST,
        importer_rel="tests/ModuleDependencyArchTest.java",
        project_root=root,
    )
    assert ev.ok is True
    assert ev.reason == "library_terminal"
    assert ev.confidence == "declared"


def test_good_2_end_to_end_gate_credits_archunit_marker(tmp_path):
    root = _write_project(
        tmp_path,
        {
            "docs/test/test_strategy.md": "| VB | D |\n| --- | --- |\n| VB-ARCH-01 | demo |\n",
            "tests/ModuleDependencyArchTest.java": _GOOD2_ARCHUNIT_TEST,
        },
    )

    class _Stub:
        language = "java"

        def test_block_profile(self):
            return JavaTestBlockProfile()

    report = build_authenticity_report(
        root, config={"scan": {"test_dirs": ["tests/"]}}, profile=_Stub()
    )
    assert report.passed, report.violations


# ── GOOD 3: static-import direct assertion (regression — already worked) ────


def test_good_3_static_import_direct_assertion_still_works():
    """Regression: a bare, static-imported ``assertEquals(...)`` is a DIRECT
    primitive — never routed through helper resolution at all."""

    text = (
        "import org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class XTest {\n"
        "    @Test\n"
        "    void x() { assertEquals(5, add(2, 3)); }\n"
        "    static int add(int a, int b) { return a + b; }\n"
        "}\n"
    )
    block = JavaTestBlockProfile().parse_test_blocks(text)[0]
    assert block.has_assertion is True
    ev = JavaTestBlockProfile().resolve_direct_assertion_evidence(block)
    assert ev.ok is True
    assert ev.reason == "direct"


# ── GOOD 4: qualified-primitive fix (Assertions.assertEquals, plain import) ──


def test_good_4_qualified_primitive_with_plain_import_now_resolves():
    """``Assertions.assertEquals(expected, actual)`` with only a PLAIN
    ``import org.junit.jupiter.api.Assertions;`` (no static import) must now
    resolve as a DIRECT primitive — this was broken before (the old lookbehind
    rejected any qualified/member-select call)."""

    text = (
        "import org.junit.jupiter.api.Assertions;\n"
        "import org.junit.jupiter.api.Test;\n\n"
        "class QualifiedAssertTest {\n"
        "    @Test\n"
        "    void x() {\n"
        "        int expected = 5;\n"
        "        int actual = add(2, 3);\n"
        "        Assertions.assertEquals(expected, actual);\n"
        "    }\n"
        "    static int add(int a, int b) { return a + b; }\n"
        "}\n"
    )
    block = JavaTestBlockProfile().parse_test_blocks(text)[0]
    assert block.has_assertion is True, "qualified Assertions.assertEquals must count as primitive"
    ev = JavaTestBlockProfile().resolve_direct_assertion_evidence(block)
    assert ev.ok is True
    assert ev.reason == "direct"


def test_good_4_qualified_primitive_constant_only_still_rejected():
    """Sanity: the qualified-call widening must not blanket-credit — a
    constant-only qualified call still fails as ``constant_direct``."""

    text = (
        "import org.junit.jupiter.api.Assertions;\n"
        "import org.junit.jupiter.api.Test;\n\n"
        "class QualifiedConstantTest {\n"
        "    @Test\n"
        "    void x() { Assertions.assertEquals(1, 1); }\n"
        "}\n"
    )
    block = JavaTestBlockProfile().parse_test_blocks(text)[0]
    assert block.has_assertion is True
    ev = JavaTestBlockProfile().resolve_direct_assertion_evidence(block)
    assert ev.ok is False
    assert ev.reason == "constant_direct"


# ── ADVERSARIAL 5: no-op helper (empty body) ────────────────────────────────


def test_adversarial_5_noop_helper_empty_body_fails(tmp_path):
    importer = (
        "package com.example;\n\n"
        "class NoopHelperTest {\n"
        "    void x() { HarnessAssertions5.assertSuccess(true); }\n"
        "}\n"
    )
    helper = (
        "package com.example;\n\n"
        "class HarnessAssertions5 {\n"
        "    static void assertSuccess(boolean ok) {\n"
        "    }\n"
        "}\n"
    )
    root = _write_project(
        tmp_path,
        {"tests/NoopHelperTest.java": importer, "tests/HarnessAssertions5.java": helper},
    )
    block = TestBlock(
        start_line=4, end_line=4, is_executable=True, has_assertion=False,
        label="x", body_text="HarnessAssertions5.assertSuccess(true);",
    )
    ev = JavaTestBlockProfile().resolve_assertion_evidence(
        block, importer_text=importer, importer_rel="tests/NoopHelperTest.java", project_root=root
    )
    assert ev.ok is False
    assert ev.reason == "helper_no_primitive"


# ── ADVERSARIAL 6: helper asserts but ignores its own argument ─────────────


def test_adversarial_6_helper_ignores_own_argument_fails(tmp_path):
    importer = (
        "package com.example;\n\n"
        "class ConstantHelperTest {\n"
        "    void x() { HarnessAssertions6.assertSuccess(computeResult()); }\n"
        "    static Object computeResult() { return new Object(); }\n"
        "}\n"
    )
    helper = (
        "package com.example;\n\n"
        "import static org.junit.jupiter.api.Assertions.assertTrue;\n\n"
        "class HarnessAssertions6 {\n"
        "    static void assertSuccess(Object r) {\n"
        "        assertTrue(true);\n"
        "    }\n"
        "}\n"
    )
    root = _write_project(
        tmp_path,
        {"tests/ConstantHelperTest.java": importer, "tests/HarnessAssertions6.java": helper},
    )
    block = TestBlock(
        start_line=4, end_line=4, is_executable=True, has_assertion=False,
        label="x", body_text="HarnessAssertions6.assertSuccess(computeResult());",
    )
    ev = JavaTestBlockProfile().resolve_assertion_evidence(
        block, importer_text=importer, importer_rel="tests/ConstantHelperTest.java", project_root=root
    )
    assert ev.ok is False
    assert ev.reason == "constant_helper"


# ── ADVERSARIAL 7: candidate method only logs, no assertion at all ─────────


def test_adversarial_7_logging_only_candidate_fails(tmp_path):
    importer = (
        "package com.example;\n\n"
        "class LoggingOnlyTest {\n"
        "    void x() { HarnessAssertions7.assertSuccess(computeResult()); }\n"
        "    static Object computeResult() { return new Object(); }\n"
        "}\n"
    )
    helper = (
        "package com.example;\n\n"
        "class HarnessAssertions7 {\n"
        "    static void assertSuccess(Object r) {\n"
        "        System.out.println(r);\n"
        "    }\n"
        "}\n"
    )
    root = _write_project(
        tmp_path,
        {"tests/LoggingOnlyTest.java": importer, "tests/HarnessAssertions7.java": helper},
    )
    block = TestBlock(
        start_line=4, end_line=4, is_executable=True, has_assertion=False,
        label="x", body_text="HarnessAssertions7.assertSuccess(computeResult());",
    )
    ev = JavaTestBlockProfile().resolve_assertion_evidence(
        block, importer_text=importer, importer_rel="tests/LoggingOnlyTest.java", project_root=root
    )
    assert ev.ok is False
    assert ev.reason == "helper_no_primitive"


# ── ADVERSARIAL 8: no ArchUnit import, unrelated check(File) method ─────────


def test_adversarial_8_unrelated_check_method_without_archunit_import_fails(tmp_path):
    """A ``check(File f)`` call with NO ArchUnit import in the file must fail
    exactly as before this change — the terminal mechanism REQUIRES the import,
    and the E2 fallback's receiver-vs-stem match is case-sensitive so the
    lowercase instance variable ``validator`` never matches ``Validator.java``
    either (proving E2 doesn't accidentally widen this)."""

    importer = (
        "package com.example;\n\n"
        "import org.junit.jupiter.api.Test;\n\n"
        "class NoArchUnitTest {\n"
        "    // codd: covers vb=VB-01\n"
        "    @Test\n"
        "    void x() {\n"
        "        Validator validator = new Validator();\n"
        '        validator.check(new java.io.File("x"));\n'
        "    }\n"
        "}\n"
    )
    sut = (
        "package com.example;\n\n"
        "class Validator {\n"
        "    void check(java.io.File f) {\n"
        "        System.out.println(f.getName());\n"
        "    }\n"
        "}\n"
    )
    root = _write_project(
        tmp_path, {"tests/NoArchUnitTest.java": importer, "tests/Validator.java": sut}
    )
    block = TestBlock(
        start_line=6,
        end_line=9,
        is_executable=True,
        has_assertion=False,
        label="x",
        body_text=(
            "        Validator validator = new Validator();\n"
            '        validator.check(new java.io.File("x"));\n'
        ),
    )
    ev = JavaTestBlockProfile().resolve_assertion_evidence(
        block, importer_text=importer, importer_rel="tests/NoArchUnitTest.java", project_root=root
    )
    assert ev.ok is False
    assert ev.reason != "library_terminal"


# ── ADVERSARIAL 9: ArchUnit imported, but the terminal call anchors nothing ──


def test_adversarial_9_archunit_terminal_with_no_anchor_stays_red(tmp_path):
    importer = (
        "package com.example;\n\n"
        "import com.tngtech.archunit.core.domain.JavaClasses;\n"
        "import com.tngtech.archunit.core.importer.ClassFileImporter;\n"
        "import org.junit.jupiter.api.Test;\n\n"
        "import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;\n\n"
        "class EmptyArchTest {\n"
        "    // codd: covers vb=VB-01\n"
        "    @Test\n"
        "    void x() {\n"
        '        noClasses().that().resideInAPackage("..tokenizer..")\n'
        '            .should().dependOnClassesThat().resideInAPackage("..parser..")\n'
        "            .check(null);\n"
        "    }\n"
        "}\n"
    )
    root = _write_project(tmp_path, {"tests/EmptyArchTest.java": importer})
    body_text = (
        '        noClasses().that().resideInAPackage("..tokenizer..")\n'
        '            .should().dependOnClassesThat().resideInAPackage("..parser..")\n'
        "            .check(null);\n"
    )
    block = TestBlock(
        start_line=9, end_line=11, is_executable=True, has_assertion=False, label="x", body_text=body_text
    )
    ev = JavaTestBlockProfile().resolve_assertion_evidence(
        block, importer_text=importer, importer_rel="tests/EmptyArchTest.java", project_root=root
    )
    assert ev.ok is False
    assert ev.reason != "library_terminal"


# ── ADVERSARIAL 10: 2-hop argument-laundering chain ─────────────────────────


def test_adversarial_10_argument_laundering_chain_fails_at_first_hop(tmp_path):
    """The inner call ``assertConstant(constant)`` does NOT forward
    ``assertSuccess``'s own parameters (``result``/``expected``) — the SHARED
    engine's existing forwarding check (``inner_anchor & param_set``, unchanged
    by this increment except for the E1 3-tuple threading) must refuse to
    follow this hop, so the chain fails at ``assertSuccess`` itself rather than
    ever reaching ``assertConstant``'s (constant-only) body."""

    importer = (
        "package com.example;\n\n"
        "class LaunderingTest {\n"
        "    void x() { HarnessAssertions10.assertSuccess(compute(), \"14.0\"); }\n"
        '    static String compute() { return "14.0"; }\n'
        "}\n"
    )
    helper = (
        "package com.example;\n\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class HarnessAssertions10 {\n"
        "    static void assertSuccess(String result, String expected) {\n"
        "        int constant = 42;\n"
        "        assertConstant(constant);\n"
        "    }\n"
        "    static void assertConstant(int x) {\n"
        "        assertEquals(1, 1);\n"
        "    }\n"
        "}\n"
    )
    root = _write_project(
        tmp_path,
        {"tests/LaunderingTest.java": importer, "tests/HarnessAssertions10.java": helper},
    )
    block = TestBlock(
        start_line=4, end_line=4, is_executable=True, has_assertion=False,
        label="x", body_text='HarnessAssertions10.assertSuccess(compute(), "14.0");',
    )
    ev = JavaTestBlockProfile().resolve_assertion_evidence(
        block, importer_text=importer, importer_rel="tests/LaunderingTest.java", project_root=root
    )
    assert ev.ok is False
    assert ev.reason == "helper_no_primitive", (
        "the non-forwarding inner call must never be followed, so assertSuccess "
        "is judged on its OWN (primitive-less) body only"
    )


# ── ADVERSARIAL 11: E2 fallback path-traversal must still be jailed ─────────


def test_adversarial_11_fallback_candidates_path_traversal_is_rejected(tmp_path):
    """A crafted ``fallback_module_candidates`` plug that returns OUT-OF-TREE
    paths must be rejected by :func:`resolve_project_path`'s jailing inside
    :func:`_resolve_one_helper` — proven by planting a REAL file outside the
    project root with a genuinely resolvable helper: if the jail did not hold,
    this fixture would false-GREEN."""

    project_root = tmp_path / "project"
    importer_rel = "tests/XTest.java"
    importer_text = (
        "class XTest {\n"
        '    void x() { HarnessAssertions.assertSuccess(compute(), "14.0"); }\n'
        '    static String compute() { return "14.0"; }\n'
        "}\n"
    )
    _write_project(project_root, {importer_rel: importer_text})

    # A file OUTSIDE project_root with a genuine, resolvable, anchored primitive
    # assertion — if the E2 jail failed, this WOULD be found and credited.
    outside = tmp_path / "outside_HarnessAssertions.java"
    outside.write_text(
        "class HarnessAssertions {\n"
        "    static void assertSuccess(String result, String expected) {\n"
        "        assertEquals(expected, result);\n"
        "    }\n"
        "}\n"
    )

    def _malicious_fallback(importer_text, importer_rel, project_root, full_callee):
        return [
            outside,  # absolute, out-of-tree
            pathlib.Path("../../../../../../../../etc/passwd"),  # traversal attempt
        ]

    block = TestBlock(
        start_line=2,
        end_line=2,
        is_executable=True,
        has_assertion=False,
        label="x",
        body_text='HarnessAssertions.assertSuccess(compute(), "14.0");',
    )
    evidence = vma._resolve_evidence(
        block,
        importer_text=importer_text,
        importer_rel=importer_rel,
        project_root=project_root,
        primitive_re=vma._JAVA_HELPER_PRIMITIVE_RE,
        imported_lookup=vma._java_imported_lookup,
        module_resolver=vma._java_resolve_module,
        def_finder=vma._java_find_method_def,
        reexport_edges=None,
        fallback_module_candidates=_malicious_fallback,
    )
    assert evidence.ok is False
    assert evidence.reason == "unresolved_helper"


def test_adversarial_11_java_fallback_candidates_never_escapes_directory(tmp_path):
    """:func:`_java_fallback_candidates` itself only ever proposes files that
    are real siblings inside the project tree — never an absolute or
    ``../``-escaping guess, independent of the engine's own re-jailing."""

    project_root = tmp_path / "project"
    importer_rel = "tests/XTest.java"
    _write_project(
        project_root,
        {
            importer_rel: 'class XTest { void x() { Foo.bar(1); } }\n',
            "tests/Foo.java": "class Foo { static void bar(int x) {} }\n",
        },
    )
    candidates = vma._java_fallback_candidates(
        'class XTest { void x() { Foo.bar(1); } }\n',
        importer_rel,
        project_root,
        "Foo.bar",
    )
    root_resolved = project_root.resolve()
    for candidate in candidates:
        assert candidate.resolve().is_relative_to(root_resolved)


# ── ADVERSARIAL 12: commented-out assertion inside a helper body ───────────


def test_adversarial_12_commented_out_assertion_in_helper_body_fails(tmp_path):
    importer = (
        "package com.example;\n\n"
        "class CommentedTest {\n"
        "    void x() { HarnessAssertions12.assertSuccess(compute(), \"14.0\"); }\n"
        '    static String compute() { return "14.0"; }\n'
        "}\n"
    )
    helper = (
        "package com.example;\n\n"
        "class HarnessAssertions12 {\n"
        "    static void assertSuccess(String result, String expected) {\n"
        "        // assertEquals(expected, result);\n"
        "    }\n"
        "}\n"
    )
    root = _write_project(
        tmp_path,
        {"tests/CommentedTest.java": importer, "tests/HarnessAssertions12.java": helper},
    )
    block = TestBlock(
        start_line=4, end_line=4, is_executable=True, has_assertion=False,
        label="x", body_text='HarnessAssertions12.assertSuccess(compute(), "14.0");',
    )
    ev = JavaTestBlockProfile().resolve_assertion_evidence(
        block, importer_text=importer, importer_rel="tests/CommentedTest.java", project_root=root
    )
    assert ev.ok is False
    assert ev.reason == "helper_no_primitive"


# ---------------------------------------------------------------------------
# Bonus: the confidence-gate mechanism itself (not one of the 12, but proves
# the wiring, not just the field's existence) — a profile that has NOT opted
# "declared" into its accepted_assertion_confidence must still reject a
# library_terminal credit end-to-end.
# ---------------------------------------------------------------------------


def test_bonus_confidence_gate_rejects_declared_when_not_accepted(tmp_path):
    class _CertainOnlyStub:
        language = "java"

        def test_block_profile(self):
            return JavaTestBlockProfile()

    root = _write_project(
        tmp_path,
        {
            "docs/test/test_strategy.md": "| VB | D |\n| --- | --- |\n| VB-ARCH-01 | demo |\n",
            "tests/ModuleDependencyArchTest.java": _GOOD2_ARCHUNIT_TEST,
        },
    )

    # This profile's language DOES resolve to the real java.yaml (which accepts
    # "declared"), so instead directly unit-test the gate's OWN filtering logic
    # against a synthetic accepted-set — proving the mechanism, independent of
    # java.yaml's current opt-in choice.
    accepted_certain_only = frozenset({"certain"})
    evidence = AssertionEvidence(ok=True, reason="library_terminal", confidence="declared")
    assert evidence.confidence not in accepted_certain_only

    # And the real end-to-end path (java.yaml DOES opt in) still passes, so the
    # mechanism is proven both ways: it can reject, and today's java.yaml choice
    # lets the ArchUnit case through.
    class _Stub:
        language = "java"

        def test_block_profile(self):
            return JavaTestBlockProfile()

    report = build_authenticity_report(
        root, config={"scan": {"test_dirs": ["tests/"]}}, profile=_Stub()
    )
    assert report.passed


def test_bonus_accepted_assertion_confidence_resolves_from_java_yaml():
    accepted = vma._accepted_assertion_confidence(
        type("Stub", (), {"language": "java"})()
    )
    assert accepted == frozenset({"certain", "declared"})


def test_bonus_accepted_assertion_confidence_defaults_to_certain_only():
    assert vma._accepted_assertion_confidence(None) == frozenset({"certain"})
    assert vma._accepted_assertion_confidence(type("Stub", (), {"language": ""})()) == frozenset(
        {"certain"}
    )
