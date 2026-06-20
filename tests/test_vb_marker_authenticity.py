"""Anti-false-green tests for the VB marker-authenticity gate.

These guard the third (and most important) part of the VB-coverage-gate design:
a ``codd: covers vb=<id>`` marker is a CLAIM that a test PROVES the behavior, so
the gate must reject markers attached to an empty test, a skipped test, or an
orphan id — the false-coverage that a naive "add a marker" feedback loop would
otherwise reward. It must NOT, however, reject genuine covering tests, and it
must GRACEFULLY DEGRADE (stage-1 only, no false-RED) for stacks/files it cannot
structurally parse.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.project_types import LayoutProfile
from codd.vb_marker_authenticity import (
    GoTestBlockProfile,
    PythonTestBlockProfile,
    TypeScriptTestBlockProfile,
    build_authenticity_report,
    format_authenticity_feedback,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _canonical(project: Path, rows: str) -> None:
    _write(project / "docs" / "test" / "test_strategy.md", "| VB | D |\n| --- | --- |\n" + rows)


PY_PROFILE = LayoutProfile(
    language="python", package_name="app", source_root="src", package_root="src/app", test_root="tests"
)
TS_PROFILE = LayoutProfile(
    language="typescript", package_name="app", source_root="src", package_root="src", test_root="tests"
)
GO_PROFILE = LayoutProfile(
    language="go", package_name="app", source_root=".", package_root=".", test_root="tests"
)


# ---------------------------------------------------------------------------
# Per-profile parser units (python)
# ---------------------------------------------------------------------------


def test_python_parser_detects_assertion_and_skip():
    text = (
        "import pytest\n"
        "\n"
        "def test_real():\n"
        "    assert add(2, 3) == 5\n"
        "\n"
        "@pytest.mark.skip(reason='later')\n"
        "def test_skipped():\n"
        "    assert add(1, 1) == 2\n"
        "\n"
        "def test_empty():\n"
        "    pass\n"
    )
    blocks = {b.label: b for b in PythonTestBlockProfile().parse_test_blocks(text)}
    assert blocks["test_real"].is_executable and blocks["test_real"].has_assertion
    assert blocks["test_skipped"].is_executable is False
    assert blocks["test_empty"].has_assertion is False


def test_python_parser_detects_pytest_raises_and_inline_skip():
    text = (
        "import pytest\n"
        "def test_raises():\n"
        "    with pytest.raises(ValueError):\n"
        "        boom()\n"
        "def test_inline_skip():\n"
        "    pytest.skip('nope')\n"
        "    assert True\n"
    )
    blocks = {b.label: b for b in PythonTestBlockProfile().parse_test_blocks(text)}
    assert blocks["test_raises"].has_assertion is True
    assert blocks["test_inline_skip"].is_executable is False


# ---------------------------------------------------------------------------
# Per-profile parser units (typescript / vitest)
# ---------------------------------------------------------------------------


def test_ts_parser_detects_expect_skip_and_empty():
    text = (
        "import { describe, it, expect } from 'vitest';\n"
        "describe('calc', () => {\n"
        "  it('adds', () => { expect(add(2,3)).toBe(5); });\n"
        "  it.skip('subtracts', () => { expect(sub(3,1)).toBe(2); });\n"
        "  it('empty', () => {});\n"
        "});\n"
    )
    blocks = TypeScriptTestBlockProfile().parse_test_blocks(text)
    # Three leaf `it` blocks (the describe is a container with children).
    leaves = [b for b in blocks if b.label == "it"]
    assert len(leaves) == 3
    adds = next(b for b in leaves if b.start_line == 3)
    subtracts = next(b for b in leaves if b.start_line == 4)
    empty = next(b for b in leaves if b.start_line == 5)
    assert adds.is_executable and adds.has_assertion
    assert subtracts.is_executable is False  # .skip
    assert empty.has_assertion is False


def test_ts_parser_inherits_skip_from_describe():
    text = (
        "import { describe, it, expect } from 'vitest';\n"
        "describe.skip('disabled group', () => {\n"
        "  it('would assert', () => { expect(1).toBe(1); });\n"
        "});\n"
    )
    leaves = [b for b in TypeScriptTestBlockProfile().parse_test_blocks(text) if b.label == "it"]
    assert len(leaves) == 1
    assert leaves[0].is_executable is False  # inherits describe.skip


# ---------------------------------------------------------------------------
# Composite gate — anti-false-green
# ---------------------------------------------------------------------------


def test_gate_passes_for_genuine_covering_tests(tmp_path):
    project = tmp_path
    _canonical(project, "| VB-01 | add |\n| VB-02 | sub |\n")
    _write(
        project / "tests" / "test_calc.py",
        "# codd: covers vb=VB-01\n"
        "def test_add():\n    assert add(2, 3) == 5\n\n"
        "# codd: covers vb=VB-02\n"
        "def test_sub():\n    assert sub(3, 1) == 2\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    report = build_authenticity_report(project, config=config, profile=PY_PROFILE)
    assert report.passed
    assert report.degraded_paths == []


def test_gate_rejects_marker_on_empty_test(tmp_path):
    project = tmp_path
    _canonical(project, "| VB-01 | add |\n")
    _write(
        project / "tests" / "test_calc.py",
        "# codd: covers vb=VB-01\ndef test_add():\n    pass\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)


def test_gate_rejects_marker_on_skipped_test(tmp_path):
    project = tmp_path
    _canonical(project, "| VB-01 | add |\n")
    _write(
        project / "tests" / "test_calc.py",
        "import pytest\n"
        "# codd: covers vb=VB-01\n"
        "@pytest.mark.skip(reason='wip')\n"
        "def test_add():\n    assert add(2, 3) == 5\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert not report.passed
    assert any(v.kind == "skipped" and v.vb_id == "VB-01" for v in report.violations)


def test_gate_rejects_orphan_marker_stage1(tmp_path):
    project = tmp_path
    _canonical(project, "| VB-01 | add |\n")
    # Marker references an UNDECLARED id; even with a real assertion it is orphan.
    _write(
        project / "tests" / "test_calc.py",
        "# codd: covers vb=VB-99\ndef test_x():\n    assert add(2, 3) == 5\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert not report.passed
    assert any(v.kind == "orphan" and v.vb_id == "VB-99" for v in report.violations)


def test_gate_rejects_marker_on_skipped_ts_test(tmp_path):
    project = tmp_path
    _canonical(project, "| VB-CLI-01 | converts |\n")
    _write(
        project / "tests" / "conv.test.ts",
        "import { describe, it, expect } from 'vitest';\n"
        "describe('cli', () => {\n"
        "  // codd: covers vb=VB-CLI-01\n"
        "  it.skip('converts', () => { expect(conv()).toBe(1); });\n"
        "});\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE)
    assert not report.passed
    assert any(v.kind == "skipped" and v.vb_id == "VB-CLI-01" for v in report.violations)


def test_gate_passes_genuine_ts_test(tmp_path):
    project = tmp_path
    _canonical(project, "| VB-CLI-01 | converts |\n")
    _write(
        project / "tests" / "conv.test.ts",
        "import { describe, it, expect } from 'vitest';\n"
        "describe('cli', () => {\n"
        "  // codd: covers vb=VB-CLI-01\n"
        "  it('converts', () => { expect(conv()).toBe(1); });\n"
        "});\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE)
    assert report.passed


# ---------------------------------------------------------------------------
# Graceful degradation — un-parseable stack must never false-RED
# ---------------------------------------------------------------------------


def test_gate_degrades_for_unknown_stack_no_false_red(tmp_path):
    project = tmp_path
    _canonical(project, "| VB-01 | rust behavior |\n")
    # A Rust test file: no adapter recognizes it → stage-1 only, stages 2-3 skip.
    _write(
        project / "tests" / "lib.rs",
        "// codd: covers vb=VB-01\n#[test]\nfn it_works() { /* no assertion seen */ }\n",
    )
    # .rs is not a recognized test suffix, so it is not even scanned → no markers,
    # no violations. The real degradation path is exercised below with a profile
    # whose adapter declines the file.
    go_profile = LayoutProfile(
        language="go", package_name="app", source_root="src", package_root="src", test_root="tests"
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=go_profile)
    assert report.passed  # no false-RED


def test_gate_degrades_when_profile_is_none(tmp_path):
    project = tmp_path
    _canonical(project, "| VB-01 | add |\n")
    # A real .py marker on an empty test, but NO profile passed → stages 2-3 skip.
    _write(
        project / "tests" / "test_x.py",
        "# codd: covers vb=VB-01\ndef test_x():\n    pass\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=None)
    # Stage 1 (orphan) passes (id is declared); stages 2-3 degrade → no violation.
    assert report.passed
    assert "tests/test_x.py" in report.degraded_paths


def test_degradation_still_catches_orphan(tmp_path):
    """Even with no profile, stage 1 (orphan, language-agnostic) still fires."""
    project = tmp_path
    _canonical(project, "| VB-01 | add |\n")
    _write(
        project / "tests" / "test_x.py",
        "# codd: covers vb=VB-77\ndef test_x():\n    pass\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=None)
    assert not report.passed
    assert any(v.kind == "orphan" for v in report.violations)


def test_feedback_is_about_strengthening_not_marking(tmp_path):
    project = tmp_path
    _canonical(project, "| VB-01 | add |\n")
    _write(
        project / "tests" / "test_x.py",
        "# codd: covers vb=VB-01\ndef test_x():\n    pass\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    feedback = format_authenticity_feedback(report)
    assert "assertion that would FAIL" in feedback
    assert "never by silencing the gate" in feedback


def test_assert_true_constant_only_marker_is_now_rejected(tmp_path):
    """CONTRACT CHANGE (constant-direct false-green closed): `def t(): assert True`
    under a `covers vb=` marker used to PASS — a primitive ``assert`` satisfied
    ``has_assertion`` and Stage 3 credited it ``direct`` unconditionally. That was
    the exact false-green a padded/fake covering test exploits (the direct-side
    analogue of the constant-only HELPER the gate already rejects via the argument
    anchor). It is now a ``constant_direct`` ⇒ ``no_assertion`` VIOLATION: a
    constant assertion references no observed result/exception/state/output, so it
    proves nothing. (Was ``test_existing_assert_true_marker_still_passes``, which
    asserted the now-removed pass.)"""
    project = tmp_path
    _canonical(project, "| VB-01 | a |\n| VB-02 | b |\n")
    _write(
        project / "tests" / "test_app.py",
        "# codd: covers vb=VB-01\n"
        "# codd: covers vb=VB-02\n"
        "def test_app():\n    assert True\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)
    assert any("CONSTANT" in v.message or "constant" in v.message for v in report.violations)


# ---------------------------------------------------------------------------
# Constant-direct false-green closure (Stage 3 direct-side argument anchor).
# A DIRECT primitive assertion is credited only when it references a non-ignored
# name (a SUT call / exception / Enum / state / output). A constant-only direct
# assertion proves nothing and is rejected — WITHOUT false-RED'ing a legitimate
# NO-FIXTURE test (which references a real call/exception/local) and WITHOUT a
# local-constant/dataflow analysis (``x = True; assert x`` stays a known residual).
# ---------------------------------------------------------------------------


def test_gate_rejects_python_direct_literal_assertion(tmp_path):
    """(a) A Python ``assert True`` under a marker is constant-only ⇒ REJECTED."""
    project = tmp_path
    _canonical(project, "| VB-01 | a |\n")
    _write(
        project / "tests" / "test_app.py",
        "# codd: covers vb=VB-01\n"
        "def test_app():\n"
        "    assert True\n",
    )

    report = build_authenticity_report(
        project,
        config={"scan": {"test_dirs": ["tests/"]}},
        profile=PY_PROFILE,
    )

    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)
    assert any("CONSTANT" in v.message or "constant" in v.message for v in report.violations)


def test_gate_rejects_ts_direct_literal_expect(tmp_path):
    """(b) A TS ``expect(true).toBe(true)`` under a marker is constant-only ⇒ REJECTED."""
    project = tmp_path
    _canonical(project, "| VB-01 | a |\n")
    _write(
        project / "tests" / "fake.test.ts",
        "import { describe, it, expect } from 'vitest';\n"
        "describe('fake', () => {\n"
        "  // codd: covers vb=VB-01\n"
        "  it('pretends', () => { expect(true).toBe(true); });\n"
        "});\n",
    )

    report = build_authenticity_report(
        project,
        config={"scan": {"test_dirs": ["tests/"]}},
        profile=TS_PROFILE,
    )

    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)


# ---------------------------------------------------------------------------
# Go (``testing`` + testify) adapter — anti-false-green parity with PY/TS.
#
# The C-Go dogfood run reported 60/60 verifiable-behaviors UNCOVERED even though
# the generated Go tests carried 132 ``// codd: covers vb=…`` markers: ``*_test.go``
# was not a scanned test suffix AND the marker/authenticity gate had no Go adapter
# (Go fell through to stage-1-only degrade). These guard BOTH halves: the coverage
# scan now SEES Go markers, and the authenticity gate rejects a Go marker on a test
# with no real assertion (or a constant-only one) while crediting a genuine one.
# ---------------------------------------------------------------------------


def test_go_parser_detects_assertion_skip_and_empty():
    """Unit: the Go adapter resolves has_assertion / skip / empty per function,
    mirroring ``test_python_parser_detects_assertion_and_skip``."""
    text = (
        "package app\n"
        "import \"testing\"\n"
        "func TestReal(t *testing.T) {\n"
        "\tgot := Add(2, 3)\n"
        "\tif got != 5 {\n"
        "\t\tt.Fatalf(\"got %d\", got)\n"
        "\t}\n"
        "}\n"
        "func TestSkipped(t *testing.T) {\n"
        "\tt.Skip(\"later\")\n"
        "\tif Add(1, 1) != 2 { t.Fatal(\"x\") }\n"
        "}\n"
        "func TestEmpty(t *testing.T) {\n"
        "\t_ = Add(1, 1)\n"
        "}\n"
    )
    blocks = {b.label: b for b in GoTestBlockProfile().parse_test_blocks(text)}
    assert blocks["TestReal"].is_executable and blocks["TestReal"].has_assertion
    assert blocks["TestSkipped"].is_executable is False
    assert blocks["TestEmpty"].has_assertion is False


def test_go_parser_detects_testify_and_subtests():
    """Unit: testify calls count as primitives; ``t.Run`` subtests are nested leaf
    blocks (the group→leaf shape the TS ``describe``→``it`` adapter uses)."""
    text = (
        "package app\n"
        "import (\n"
        "\t\"testing\"\n"
        "\t\"github.com/stretchr/testify/require\"\n"
        ")\n"
        "func TestTestify(t *testing.T) {\n"
        "\tgot := Add(1, 2)\n"
        "\trequire.Equal(t, 3, got)\n"
        "}\n"
        "func TestGroup(t *testing.T) {\n"
        "\tt.Run(\"adds\", func(t *testing.T) {\n"
        "\t\tgot := Add(1, 1)\n"
        "\t\tif got != 2 { t.Fatalf(\"got %d\", got) }\n"
        "\t})\n"
        "\tt.Run(\"skipped\", func(t *testing.T) {\n"
        "\t\tt.Skip()\n"
        "\t})\n"
        "}\n"
    )
    blocks = GoTestBlockProfile().parse_test_blocks(text)
    by_label = {b.label: b for b in blocks if b.label == "TestTestify"}
    assert by_label["TestTestify"].has_assertion is True
    subtests = [b for b in blocks if b.label == "TestGroup/subtest"]
    assert len(subtests) == 2
    adds = min(subtests, key=lambda b: b.start_line)
    skipped = max(subtests, key=lambda b: b.start_line)
    assert adds.is_executable and adds.has_assertion
    assert skipped.is_executable is False  # t.Skip()
    # The grouping function's OWN facts are NOT inherited from its children: it is
    # not itself skipped (a child is) and carries no direct assertion of its own.
    group = next(b for b in blocks if b.label == "TestGroup")
    assert group.is_executable is True
    assert group.has_assertion is False


def test_go_gate_credits_real_guarded_assertion(tmp_path):
    """marker + real assertion ``if got != want { t.Fatalf(...) }`` (got from a SUT
    call) ⇒ CREDITED (report.passed), no degradation."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | add |\n")
    _write(
        project / "tests" / "add_test.go",
        "package app\n"
        "import \"testing\"\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestAdd(t *testing.T) {\n"
        "\tgot := Add(2, 3)\n"
        "\twant := 5\n"
        "\tif got != want {\n"
        "\t\tt.Fatalf(\"Add(2,3) = %d, want %d\", got, want)\n"
        "\t}\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert report.passed
    assert report.degraded_paths == []  # Go file is RECOGNIZED, not degraded


def test_go_gate_credits_testify_require(tmp_path):
    """marker + testify ``require.Equal(t, want, got)`` (non-constant args) ⇒ CREDITED."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | add |\n")
    _write(
        project / "tests" / "req_test.go",
        "package app\n"
        "import (\n"
        "\t\"testing\"\n"
        "\t\"github.com/stretchr/testify/require\"\n"
        ")\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestAdd(t *testing.T) {\n"
        "\tgot := Add(2, 3)\n"
        "\trequire.Equal(t, 5, got)\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert report.passed


def test_go_gate_rejects_no_assertion(tmp_path):
    """marker + NO assertion (``func TestX(t *testing.T){ _ = Sut() }``) ⇒ REJECTED.
    This is the cardinal anti-false-green case: running code without checking an
    outcome proves nothing."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | x |\n")
    _write(
        project / "tests" / "x_test.go",
        "package app\n"
        "import \"testing\"\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestX(t *testing.T) {\n"
        "\t_ = Sut()\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-GO-01" for v in report.violations)


def test_go_gate_rejects_constant_only_assertion(tmp_path):
    """marker + constant-only (``if 1 != 1 { t.Fatal() }``) ⇒ REJECTED. A constant
    condition can never fail, so the ``t.Fatal`` is unreachable-by-design — it
    proves nothing (the Go analogue of ``assert True`` / ``expect(true).toBe(true)``)."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | c |\n")
    _write(
        project / "tests" / "c_test.go",
        "package app\n"
        "import \"testing\"\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestC(t *testing.T) {\n"
        "\tif 1 != 1 {\n"
        "\t\tt.Fatal(\"never\")\n"
        "\t}\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-GO-01" for v in report.violations)
    assert any("CONSTANT" in v.message or "constant" in v.message for v in report.violations)


def test_go_gate_rejects_unconditional_fatal_todo(tmp_path):
    """marker + unconditional ``t.Fatal("todo")`` (no SUT/expected reference) ⇒
    REJECTED — a stub failure call with no observation is not a real assertion."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | todo |\n")
    _write(
        project / "tests" / "todo_test.go",
        "package app\n"
        "import \"testing\"\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestTodo(t *testing.T) {\n"
        "\tt.Fatal(\"todo\")\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-GO-01" for v in report.violations)


def test_go_gate_rejects_constant_testify(tmp_path):
    """marker + testify with only constant args (``assert.Equal(t, 1, 1)``) ⇒ REJECTED."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | c |\n")
    _write(
        project / "tests" / "ct_test.go",
        "package app\n"
        "import (\n"
        "\t\"testing\"\n"
        "\t\"github.com/stretchr/testify/assert\"\n"
        ")\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestConst(t *testing.T) {\n"
        "\tassert.Equal(t, 1, 1)\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-GO-01" for v in report.violations)


def test_go_gate_rejects_marker_on_skipped_test(tmp_path):
    """marker + ``t.Skip()`` ⇒ handled like other skipped blocks (``skipped`` kind),
    matching Python ``pytest.skip`` / TS ``it.skip`` semantics — a skipped test
    asserts nothing even though it contains a real ``t.Fatal`` guard."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | s |\n")
    _write(
        project / "tests" / "s_test.go",
        "package app\n"
        "import \"testing\"\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestSkipped(t *testing.T) {\n"
        "\tt.Skip(\"wip\")\n"
        "\tgot := Add(1, 1)\n"
        "\tif got != 2 { t.Fatalf(\"got %d\", got) }\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "skipped" and v.vb_id == "VB-GO-01" for v in report.violations)


def test_go_gate_credits_resolved_assertion_helper(tmp_path):
    """marker + a bare assertion-helper call whose same-file ``func`` body runs a
    real ``t.Fatalf`` anchored on the helper's args ⇒ CREDITED via the 1-hop helper
    graph (the Go analogue of the PY/TS ``expectSuccessfulRun`` delegation)."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | h |\n")
    _write(
        project / "tests" / "h_test.go",
        "package app\n"
        "import \"testing\"\n"
        "func checkEqual(t *testing.T, got int, want int) {\n"
        "\tif got != want {\n"
        "\t\tt.Fatalf(\"got %d want %d\", got, want)\n"
        "\t}\n"
        "}\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestViaHelper(t *testing.T) {\n"
        "\tgot := Add(2, 3)\n"
        "\tcheckEqual(t, got, 5)\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert report.passed


def test_go_gate_rejects_noop_assertion_helper(tmp_path):
    """marker + a bare helper call whose body has NO assertion ⇒ REJECTED — a helper
    that checks nothing proves nothing (argument-anchor / no-op defense, mirroring
    the PY/TS helper-no-primitive rejection)."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | h |\n")
    _write(
        project / "tests" / "noop_test.go",
        "package app\n"
        "import \"testing\"\n"
        "func checkNothing(t *testing.T, got int) {\n"
        "\t_ = got\n"
        "}\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestNoop(t *testing.T) {\n"
        "\tgot := Add(1, 1)\n"
        "\tcheckNothing(t, got)\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-GO-01" for v in report.violations)


def test_go_gate_rejects_comment_fake_assertion(tmp_path):
    """marker + a real assertion written ONLY in a COMMENT
    (``// if got != want { t.Fatalf(...) }``) while the executable code is
    constant-only ⇒ REJECTED. A comment proves nothing; the scanner must read a
    comment-stripped skeleton (false-GREEN guard)."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | c |\n")
    _write(
        project / "tests" / "cmt_test.go",
        "package app\n"
        "import \"testing\"\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestCommentFake(t *testing.T) {\n"
        "\t// if got != want { t.Fatalf(\"...\") }\n"
        "\tif 1 != 1 {\n"
        "\t\tt.Fatal()\n"
        "\t}\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-GO-01" for v in report.violations)


def test_go_gate_comment_mentioning_skip_is_not_skip(tmp_path):
    """NO-FALSE-RED guard: a real, asserting test with a COMMENT that merely
    mentions ``t.Skip()`` must NOT be treated as skipped (comment-stripped skip
    detection)."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | add |\n")
    _write(
        project / "tests" / "note_test.go",
        "package app\n"
        "import \"testing\"\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestNote(t *testing.T) {\n"
        "\t// t.Skip() — TODO note, not an actual skip\n"
        "\tgot := Add(2, 3)\n"
        "\tif got != 5 { t.Fatalf(\"got %d\", got) }\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert report.passed


def test_go_gate_credits_err_nil_idiom(tmp_path):
    """NO-FALSE-RED guard: the most common Go assertion idiom
    ``if err != nil { t.Fatalf(...) }`` references ``err`` (non-constant) and must
    stay GREEN — the Go analogue of the PY/TS no-fixture-exception guard."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | parse |\n")
    _write(
        project / "tests" / "parse_test.go",
        "package app\n"
        "import \"testing\"\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestParse(t *testing.T) {\n"
        "\t_, err := Parse(\"x\")\n"
        "\tif err != nil {\n"
        "\t\tt.Fatalf(\"unexpected: %v\", err)\n"
        "\t}\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert report.passed


def test_go_gate_credits_aliased_testify(tmp_path):
    """NO-FALSE-RED guard: a testify package imported under an ALIAS
    (``treq "…/require"``; ``treq.Equal(t, want, got)``) is still recognized as a
    real assertion — the alias import lives OUTSIDE the function body, so alias
    resolution must read the whole file (regression guard for that bug)."""
    project = tmp_path
    _canonical(project, "| VB-GO-01 | add |\n")
    _write(
        project / "tests" / "alias_test.go",
        "package app\n"
        "import (\n"
        "\t\"testing\"\n"
        "\ttreq \"github.com/stretchr/testify/require\"\n"
        ")\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestAliased(t *testing.T) {\n"
        "\tgot := Add(2, 3)\n"
        "\ttreq.Equal(t, 5, got)\n"
        "}\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert report.passed


def test_go_coverage_finds_markers_present(tmp_path):
    """THE ORIGINAL C-GO 60/60 BUG: markers PRESENT in a Go test must be FOUND.

    Before the fix ``*_test.go`` was not a scanned suffix, so every Go ``covers``
    marker went unread and declared behaviors read as uncovered. Here two real,
    correctly-marked Go tests must reconcile their VBs as COVERED (zero uncovered),
    proving the markers are now scanned/found."""
    from codd.verifiable_behavior_audit import build_vb_coverage_audit

    project = tmp_path
    _canonical(project, "| VB-GO-01 | add |\n| VB-GO-02 | sub |\n")
    _write(
        project / "tests" / "calc_test.go",
        "package app\n"
        "import \"testing\"\n"
        "// codd: covers vb=VB-GO-01\n"
        "func TestAdd(t *testing.T) {\n"
        "\tgot := Add(2, 3)\n"
        "\tif got != 5 { t.Fatalf(\"got %d\", got) }\n"
        "}\n"
        "// codd: covers vb=VB-GO-02\n"
        "func TestSub(t *testing.T) {\n"
        "\tgot := Sub(3, 1)\n"
        "\tif got != 2 { t.Fatalf(\"got %d\", got) }\n"
        "}\n",
    )
    report = build_vb_coverage_audit(project, config={"scan": {"test_dirs": ["tests/"]}})
    assert report.summary["uncovered"] == 0
    assert report.summary["covered"] == 2
    # And the authenticity gate credits both (real guarded assertions).
    auth = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=GO_PROFILE
    )
    assert auth.passed


def test_gate_keeps_python_no_fixture_exception_assertion_green(tmp_path):
    """(c) CRITICAL no-false-RED guard: a NO-FIXTURE exception assertion
    (``assert exc.value.code == ErrorCode.Y``) references ``exc`` — a non-ignored
    name — so it is a REAL observation and must stay GREEN. This is the legitimate
    test shape that a naive "exclude all call/exception names" filter would
    false-RED; the ignored set deliberately keeps SUT/exception/Enum names."""
    project = tmp_path
    _canonical(project, "| VB-ERR-01 | unknown category rejected |\n")
    _write(
        project / "tests" / "test_classification.py",
        "import pytest\n"
        "\n"
        "class ValidationFailure(Exception):\n"
        "    def __init__(self, code):\n"
        "        self.code = code\n"
        "\n"
        "class ErrorCode:\n"
        "    ERR_UNKNOWN_CATEGORY = 'unknown-category'\n"
        "\n"
        "def parse_classification_output(payload):\n"
        "    raise ValidationFailure(ErrorCode.ERR_UNKNOWN_CATEGORY)\n"
        "\n"
        "# codd: covers vb=VB-ERR-01\n"
        "def test_unknown_category_is_rejected():\n"
        "    with pytest.raises(ValidationFailure) as exc:\n"
        "        parse_classification_output({'category': 'invalid'})\n"
        "    assert exc.value.code == ErrorCode.ERR_UNKNOWN_CATEGORY\n",
    )

    report = build_authenticity_report(
        project,
        config={"scan": {"test_dirs": ["tests/"]}},
        profile=PY_PROFILE,
    )

    assert report.passed, [v.message for v in report.violations]


def test_gate_keeps_ts_direct_observation_green(tmp_path):
    """(d) A TS direct observation ``expect(conv()).toBe(1)`` references ``conv``
    (a SUT call) — a real observation, so it stays GREEN. Only constants are
    rejected; a call result is not a constant."""
    project = tmp_path
    _canonical(project, "| VB-01 | converts |\n")
    _write(
        project / "tests" / "conv.test.ts",
        "import { describe, it, expect } from 'vitest';\n"
        "function conv(): number { return 1; }\n"
        "describe('cli', () => {\n"
        "  // codd: covers vb=VB-01\n"
        "  it('converts', () => { expect(conv()).toBe(1); });\n"
        "});\n",
    )

    report = build_authenticity_report(
        project,
        config={"scan": {"test_dirs": ["tests/"]}},
        profile=TS_PROFILE,
    )

    assert report.passed, [v.message for v in report.violations]


@pytest.mark.xfail(
    reason="Stage 3 direct-assertion filter intentionally does NOT perform local "
    "constant/dataflow analysis. ``x = True; assert x`` is a documented residual "
    "false-green — closing it (subtracting literal-bound locals from the anchor) "
    "would risk false-RED on legitimate callback/mutation tests "
    "(``called = False; ...; assert called``).",
    strict=False,
)
def test_gate_rejects_direct_local_constant_alias_future(tmp_path):
    """Documents the deliberate residual: a constant aliased through a local
    (``x = True; assert x``) is NOT yet rejected. Marked xfail (non-strict) so it
    records the known limitation without failing the suite, and would flip to a
    real pass only if a future stage adds safe dataflow without false-RED risk."""
    project = tmp_path
    _canonical(project, "| VB-01 | a |\n")
    _write(
        project / "tests" / "test_app.py",
        "# codd: covers vb=VB-01\n"
        "def test_app():\n"
        "    x = True\n"
        "    assert x\n",
    )

    report = build_authenticity_report(
        project,
        config={"scan": {"test_dirs": ["tests/"]}},
        profile=PY_PROFILE,
    )

    assert not report.passed


# ===========================================================================
# Round-2 precision: assertion EVIDENCE graph (helper delegation, 1-hop) +
# block-ized attachment. These guard the codex13 false-RED fix WITHOUT opening
# a false-green hole (the gate is widened to follow helper bodies, never weakened
# to pass on a helper NAME). See `/tmp/gpt_vb2_result.txt` (round-2 design).
# ===========================================================================


_E2E_ASSERTIONS_HELPER = (
    "import { expect } from 'vitest';\n"
    "import type { CliRunResult } from './cli.js';\n"
    "export function expectExitCode(result: CliRunResult, exitCode: number): void {\n"
    "  expect(result.exitCode).toBe(exitCode);\n"
    "}\n"
    "export function expectTrimmedStdout(result: CliRunResult, expected: string): void {\n"
    "  expect(result.stdout.trim()).toBe(expected);\n"
    "}\n"
    "export function expectSuccessfulRun(result: CliRunResult, expectedStdout: string): void {\n"
    "  expectExitCode(result, 0);\n"
    "  expectTrimmedStdout(result, expectedStdout);\n"
    "}\n"
    "export function expectRejectedRun(\n"
    "  result: CliRunResult,\n"
    "  reasons: readonly string[],\n"
    "  exitCode = 1\n"
    "): void {\n"
    "  expectExitCode(result, exitCode);\n"
    "}\n"
)


def test_gate_passes_grouped_markers_with_helper_delegated_assertion(tmp_path):
    """THE codex13 false-RED: 7 markers stacked above one `it()` whose body
    delegates its assertion to an imported helper (`expectSuccessfulRun`, itself
    delegating one hop to `expectExitCode` which runs a real `expect()` on its
    argument). Every marker must attach (no fixed lookahead) AND the helper-body
    assertion must count as evidence. This is the exact pattern that produced the
    false-RED before round-2; it must now PASS."""
    project = tmp_path
    _canonical(
        project,
        "| VB-CONV-01 | a |\n| VB-CONV-02 | b |\n| VB-CONV-03 | c |\n"
        "| VB-CONV-04 | d |\n| VB-CONV-05 | e |\n| VB-CONV-06 | f |\n| VB-CONV-07 | g |\n",
    )
    _write(project / "tests" / "e2e" / "helpers" / "assertions.ts", _E2E_ASSERTIONS_HELPER)
    _write(
        project / "tests" / "e2e" / "conv.e2e.test.ts",
        "import { describe, it } from 'vitest';\n"
        "import { expectSuccessfulRun } from './helpers/assertions.js';\n"
        "describe('conversions', () => {\n"
        "  // codd: covers vb=VB-CONV-01\n"
        "  // codd: covers vb=VB-CONV-02\n"
        "  // codd: covers vb=VB-CONV-03\n"
        "  // codd: covers vb=VB-CONV-04\n"
        "  // codd: covers vb=VB-CONV-05\n"
        "  // codd: covers vb=VB-CONV-06\n"
        "  // codd: covers vb=VB-CONV-07\n"
        "  it('runs the fixtures', async () => {\n"
        "    const result = await runIt();\n"
        "    expectSuccessfulRun(result, '212.00 F');\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]
    assert report.degraded_paths == []


def test_gate_passes_two_hop_rejected_run_helper(tmp_path):
    """A helper whose body has NO direct primitive, only delegating to a deeper
    assertion helper (expectRejectedRun -> expectExitCode), must still resolve as
    evidence (1 extra hop)."""
    project = tmp_path
    _canonical(project, "| VB-REJ-01 | rejects |\n")
    _write(project / "tests" / "e2e" / "helpers" / "assertions.ts", _E2E_ASSERTIONS_HELPER)
    _write(
        project / "tests" / "e2e" / "reject.e2e.test.ts",
        "import { describe, it } from 'vitest';\n"
        "import { expectRejectedRun } from './helpers/assertions.js';\n"
        "describe('rejects', () => {\n"
        "  // codd: covers vb=VB-REJ-01\n"
        "  it('rejects bad args', async () => {\n"
        "    const result = await runIt();\n"
        "    expectRejectedRun(result, ['usage']);\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]


def test_gate_fails_constant_only_helper_spam(tmp_path):
    """ANTI-FALSE-GREEN: a no-op helper that asserts only a CONSTANT
    (`expect(true).toBe(true)`, never referencing its argument) with markers
    spammed on the calling test must FAIL — the helper NAME is not trusted, the
    body's argument-anchor is."""
    project = tmp_path
    _canonical(project, "| VB-FAKE-01 | a |\n| VB-FAKE-02 | b |\n")
    _write(
        project / "tests" / "helpers" / "fake.ts",
        "import { expect } from 'vitest';\n"
        "export function expectSuccess(result: unknown): void {\n"
        "  expect(true).toBe(true);\n"
        "}\n",
    )
    _write(
        project / "tests" / "fake.test.ts",
        "import { describe, it } from 'vitest';\n"
        "import { expectSuccess } from './helpers/fake.js';\n"
        "describe('fake', () => {\n"
        "  // codd: covers vb=VB-FAKE-01\n"
        "  // codd: covers vb=VB-FAKE-02\n"
        "  it('pretends to test', () => {\n"
        "    const result = run();\n"
        "    expectSuccess(result);\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert not report.passed
    kinds = {(v.vb_id, v.kind) for v in report.violations}
    assert ("VB-FAKE-01", "no_assertion") in kinds
    assert ("VB-FAKE-02", "no_assertion") in kinds
    assert any("CONSTANT" in v.message for v in report.violations)


def test_gate_fails_unresolved_helper_greenfield_strict(tmp_path):
    """ANTI-FALSE-GREEN: an assertion-like call whose helper cannot be resolved
    (no import binds it / the module is absent) is NOT evidence in greenfield
    strict — it must FAIL (design: 'unresolved helper = fail')."""
    project = tmp_path
    _canonical(project, "| VB-UNRES-01 | a |\n")
    _write(
        project / "tests" / "unres.test.ts",
        "import { describe, it } from 'vitest';\n"
        "describe('unres', () => {\n"
        "  // codd: covers vb=VB-UNRES-01\n"
        "  it('calls a phantom helper', () => {\n"
        "    const result = run();\n"
        "    expectEverythingIsFine(result);\n"  # not imported, not defined anywhere
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-UNRES-01" for v in report.violations)
    assert any("could not be resolved" in v.message for v in report.violations)


def test_gate_fails_helper_with_no_primitive(tmp_path):
    """ANTI-FALSE-GREEN: a resolvable helper whose body runs NO primitive
    assertion at all (it just logs / does work) is not evidence — FAIL."""
    project = tmp_path
    _canonical(project, "| VB-NOOP-01 | a |\n")
    _write(
        project / "tests" / "helpers" / "noop.ts",
        "export function checkResult(result: unknown): void {\n"
        "  console.log(result);\n"  # no assertion, no fail
        "}\n",
    )
    _write(
        project / "tests" / "noop.test.ts",
        "import { describe, it } from 'vitest';\n"
        "import { checkResult } from './helpers/noop.js';\n"
        "describe('noop', () => {\n"
        "  // codd: covers vb=VB-NOOP-01\n"
        "  it('checks nothing', () => {\n"
        "    const result = run();\n"
        "    checkResult(result);\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-NOOP-01" for v in report.violations)


def test_gate_marker_block_not_attached_across_non_test_statement(tmp_path):
    """ATTACHMENT: a contiguous marker block attaches to the NEXT test ONLY when
    no real statement intervenes. A `const fixtures = ...` between the markers and
    the first test means the markers are a file-top banner → unattached (the
    file-top marker must not ride an import/const into a later test)."""
    project = tmp_path
    _canonical(project, "| VB-TOP-01 | a |\n")
    _write(
        project / "tests" / "banner.test.ts",
        "import { describe, it, expect } from 'vitest';\n"
        "// codd: covers vb=VB-TOP-01\n"
        "const fixtures = [1, 2, 3];\n"  # a real statement separates marker from test
        "describe('later', () => {\n"
        "  it('adds', () => { expect(add(1, 2)).toBe(3); });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "unattached" and v.vb_id == "VB-TOP-01" for v in report.violations)


def test_gate_marker_block_attaches_over_comments_and_blanks(tmp_path):
    """ATTACHMENT: marker + ordinary comments + blank lines above a test all
    attach to it (no fixed line lookahead), even when far more than 3 lines."""
    project = tmp_path
    _canonical(project, "| VB-FAR-01 | a |\n")
    _write(
        project / "tests" / "far.test.ts",
        "import { describe, it, expect } from 'vitest';\n"
        "describe('far', () => {\n"
        "  // codd: covers vb=VB-FAR-01\n"
        "  // a long\n"
        "  // explanatory\n"
        "  // comment\n"
        "  //\n"
        "  // spanning many lines\n"
        "  it('adds', () => { expect(add(1, 2)).toBe(3); });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]


def test_gate_describe_marker_attaches_to_first_it_only(tmp_path):
    """ATTACHMENT: a marker directly above a `describe` containing several `it`s
    attaches to the FIRST `it` only (no group-level fan-out / false coverage).
    The first `it` asserts, so the marker is authentic; the gate does not silently
    spread one marker across every child."""
    project = tmp_path
    _canonical(project, "| VB-GRP-01 | a |\n")
    _write(
        project / "tests" / "grp.test.ts",
        "import { describe, it, expect } from 'vitest';\n"
        "// codd: covers vb=VB-GRP-01\n"
        "describe('group', () => {\n"
        "  it('first asserts', () => { expect(a()).toBe(1); });\n"
        "  it('second asserts', () => { expect(b()).toBe(2); });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]


def test_gate_describe_marker_first_it_empty_fails(tmp_path):
    """ATTACHMENT corollary: if the marker-above-describe binds to the FIRST it
    and that first it has no assertion, the gate FAILS (it attributes the marker
    to the first child specifically, not to whichever child happens to assert)."""
    project = tmp_path
    _canonical(project, "| VB-GRP-02 | a |\n")
    _write(
        project / "tests" / "grp2.test.ts",
        "import { describe, it, expect } from 'vitest';\n"
        "// codd: covers vb=VB-GRP-02\n"
        "describe('group', () => {\n"
        "  it('first is empty', () => {});\n"
        "  it('second asserts', () => { expect(b()).toBe(2); });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-GRP-02" for v in report.violations)


# ── python parity for the evidence graph ────────────────────────────────────


def test_gate_python_helper_delegation_passes(tmp_path):
    """Python parity: a test that delegates its assertion to an imported helper
    whose body asserts on its argument resolves as evidence."""
    project = tmp_path
    _canonical(project, "| VB-PY-01 | a |\n")
    _write(
        project / "tests" / "helpers" / "__init__.py",
        "",
    )
    _write(
        project / "tests" / "helpers" / "asserts.py",
        "def expect_ok(result):\n    assert result.code == 0\n",
    )
    _write(
        project / "tests" / "test_py_helper.py",
        "from tests.helpers.asserts import expect_ok\n"
        "# codd: covers vb=VB-PY-01\n"
        "def test_runs():\n    result = run()\n    expect_ok(result)\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]


def test_gate_python_constant_helper_fails(tmp_path):
    """Python parity (anti-false-green): a helper that asserts only a constant
    (never its argument) does not make a marker authentic."""
    project = tmp_path
    _canonical(project, "| VB-PY-02 | a |\n")
    _write(project / "tests" / "helpers" / "__init__.py", "")
    _write(
        project / "tests" / "helpers" / "fake.py",
        "def verify_ok(result):\n    assert True\n",
    )
    _write(
        project / "tests" / "test_py_fake.py",
        "from tests.helpers.fake import verify_ok\n"
        "# codd: covers vb=VB-PY-02\n"
        "def test_runs():\n    result = run()\n    verify_ok(result)\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-PY-02" for v in report.violations)


def test_codex13_real_project_passes():
    """End-to-end against the actual codex13 greenfield output (the source of the
    false-RED). The whole project's `covers vb=` markers must be authentic: 0
    violations, 0 degraded. Skips cleanly if the fixture project is absent."""
    import os

    root = Path("/home/tono/codd-greenfield-tempconv-codex13")
    if not (root / "codd" / "codd.yaml").is_file():
        import pytest

        pytest.skip("codex13 fixture project not present")
    from codd.config import load_project_config

    config = load_project_config(root)
    profile = LayoutProfile(
        language="typescript",
        package_name="tempconv",
        source_root="src",
        package_root="src",
        test_root="tests",
    )
    report = build_authenticity_report(root, config=config, profile=profile)
    assert report.passed, [v.message for v in report.violations]
    assert report.degraded_paths == []


# ===========================================================================
# Round-3 reach: BARREL re-export following. The conventional e2e shape imports
# its assertion helpers from a barrel index (`import { expectSuccessResult } from
# "./helpers"` → `helpers/index.ts` does `export * from "./assertions"` → the
# real `expectSuccessResult` body). 2.31.0 resolved the import to the BARREL,
# found no def there, and FAILED (`unresolved_helper`) — the codex16 false-RED
# (54 genuine e2e markers wrongly failed). The gate now FOLLOWS the barrel's
# re-exports to the defining module, WITHOUT weakening the anti-false-green
# contract: the real body must still carry a primitive assertion + argument
# anchor, an unreachable / depth-exhausted / cyclic re-export is still a fail,
# and a barrel re-exporting a no-op/constant helper still fails.
# ===========================================================================


_BARREL_ASSERTIONS = (
    "import { expect } from 'vitest';\n"
    "import type { CliRunResult } from './cli.js';\n"
    "export function expectSuccessResult(result: CliRunResult, stdout: string): void {\n"
    "  expect(result.status).toBe(0);\n"
    "  expect(result.stdout).toBe(stdout);\n"
    "}\n"
)
#: A barrel index that ONLY re-exports siblings (no def of its own) — the exact
#: shape codex16 generated (`tests/e2e/helpers/index.ts`).
_BARREL_INDEX_STAR = (
    "export * from './assertions';\n"
    "export * from './cli';\n"
    "export * from './workspace';\n"
)


def test_gate_passes_barrel_star_reexport(tmp_path):
    """THE codex16 false-RED: a test imports its assertion helper from a barrel
    (`from "./helpers"`) whose `index.ts` only `export * from "./assertions"`.
    2.31.0 stopped at the barrel (no def there) → unresolved → fail. The gate now
    follows the star re-export to `assertions.ts` and finds the real
    `expectSuccessResult` (primitive `expect` on its argument) → PASS."""
    project = tmp_path
    _canonical(project, "| VB-RERUN-001 | reruns |\n")
    _write(project / "tests" / "e2e" / "helpers" / "assertions.ts", _BARREL_ASSERTIONS)
    _write(project / "tests" / "e2e" / "helpers" / "index.ts", _BARREL_INDEX_STAR)
    _write(
        project / "tests" / "e2e" / "rerun.e2e.test.ts",
        "import { describe, it } from 'vitest';\n"
        'import { expectSuccessResult } from "./helpers";\n'
        "describe('rerun', () => {\n"
        "  // codd: covers vb=VB-RERUN-001\n"
        "  it('reruns', async () => {\n"
        "    const result = await runIt();\n"
        "    expectSuccessResult(result, '212.00 F');\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]
    assert report.degraded_paths == []


def test_gate_fails_barrel_reexporting_constant_helper(tmp_path):
    """ANTI-FALSE-GREEN: following a barrel does NOT relax the body check. A
    barrel that `export *`s a no-op helper (`expect(true).toBe(true)`, never its
    argument) is reached but still FAILS — barrel following adds reach only."""
    project = tmp_path
    _canonical(project, "| VB-FAKE-01 | a |\n")
    _write(
        project / "tests" / "e2e" / "helpers" / "fake-assertions.ts",
        "import { expect } from 'vitest';\n"
        "export function expectSuccessResult(result: unknown): void {\n"
        "  expect(true).toBe(true);\n"  # constant — no argument anchor
        "}\n",
    )
    _write(
        project / "tests" / "e2e" / "helpers" / "index.ts",
        "export * from './fake-assertions';\n",
    )
    _write(
        project / "tests" / "e2e" / "fake.e2e.test.ts",
        "import { describe, it } from 'vitest';\n"
        'import { expectSuccessResult } from "./helpers";\n'
        "describe('fake', () => {\n"
        "  // codd: covers vb=VB-FAKE-01\n"
        "  it('pretends', () => {\n"
        "    const result = run();\n"
        "    expectSuccessResult(result);\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-FAKE-01" for v in report.violations)
    assert any("CONSTANT" in v.message for v in report.violations)


def test_gate_fails_barrel_reexporting_no_primitive_helper(tmp_path):
    """ANTI-FALSE-GREEN: a barrel reaching a helper whose body has NO primitive
    assertion at all (just logs) still fails — the reached body is empty of
    evidence."""
    project = tmp_path
    _canonical(project, "| VB-NOOP-01 | a |\n")
    _write(
        project / "tests" / "helpers" / "real.ts",
        "export function checkResult(result: unknown): void {\n"
        "  console.log(result);\n"  # no assertion
        "}\n",
    )
    _write(project / "tests" / "helpers" / "index.ts", "export * from './real';\n")
    _write(
        project / "tests" / "noop.e2e.test.ts",
        "import { describe, it } from 'vitest';\n"
        'import { checkResult } from "./helpers";\n'
        "describe('noop', () => {\n"
        "  // codd: covers vb=VB-NOOP-01\n"
        "  it('checks nothing', () => {\n"
        "    const result = run();\n"
        "    checkResult(result);\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-NOOP-01" for v in report.violations)


def test_gate_passes_barrel_named_alias_reexport(tmp_path):
    """`export { expectOk as expectSuccessResult } from "./asserts"` — a named
    re-export with an ALIAS. The test's local name is the alias; following the
    edge must look for the ORIGINAL name (`expectOk`) in the target module."""
    project = tmp_path
    _canonical(project, "| VB-ALIAS-01 | a |\n")
    _write(
        project / "tests" / "helpers" / "asserts.ts",
        "import { expect } from 'vitest';\n"
        "export function expectOk(result: { code: number }): void {\n"
        "  expect(result.code).toBe(0);\n"
        "}\n",
    )
    _write(
        project / "tests" / "helpers" / "index.ts",
        'export { expectOk as expectSuccessResult } from "./asserts";\n',
    )
    _write(
        project / "tests" / "alias.e2e.test.ts",
        "import { describe, it } from 'vitest';\n"
        'import { expectSuccessResult } from "./helpers";\n'
        "describe('alias', () => {\n"
        "  // codd: covers vb=VB-ALIAS-01\n"
        "  it('aliased', () => {\n"
        "    const result = run();\n"
        "    expectSuccessResult(result);\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]


def test_gate_passes_nested_barrel_reexport(tmp_path):
    """A barrel that re-exports ANOTHER barrel (`helpers/index` → `assertions/index`
    → `core`). Following must recurse across barrel-to-barrel edges (bounded)."""
    project = tmp_path
    _canonical(project, "| VB-NEST-01 | a |\n")
    _write(
        project / "tests" / "helpers" / "assertions" / "core.ts",
        "import { expect } from 'vitest';\n"
        "export function expectOk(result: { code: number }): void {\n"
        "  expect(result.code).toBe(0);\n"
        "}\n",
    )
    _write(
        project / "tests" / "helpers" / "assertions" / "index.ts",
        "export * from './core';\n",
    )
    _write(project / "tests" / "helpers" / "index.ts", "export * from './assertions';\n")
    _write(
        project / "tests" / "nest.e2e.test.ts",
        "import { describe, it } from 'vitest';\n"
        'import { expectOk } from "./helpers";\n'
        "describe('nest', () => {\n"
        "  // codd: covers vb=VB-NEST-01\n"
        "  it('nested barrel', () => {\n"
        "    const result = run();\n"
        "    expectOk(result);\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]


def test_gate_fails_barrel_chain_exceeding_depth(tmp_path):
    """BOUNDED DEPTH: a re-export chain LONGER than the budget (a line of barrels
    each forwarding to the next, def only at the far end) must NOT resolve — the
    follow is bounded, so an over-deep chain is `unresolved_helper` → fail. This
    proves the bound is real (not just a cycle guard)."""
    project = tmp_path
    _canonical(project, "| VB-DEEP-01 | a |\n")
    # Build a chain of barrels longer than _MAX_REEXPORT_HOPS, with the real def
    # only at the very end so resolution MUST exhaust the budget first.
    from codd.vb_marker_authenticity import _MAX_REEXPORT_HOPS

    chain_len = _MAX_REEXPORT_HOPS + 2
    helpers = project / "tests" / "helpers"
    # b0 (entry barrel imported by the test) → b1 → ... → b{n-1} → leaf def
    for i in range(chain_len):
        _write(helpers / f"b{i}.ts", f"export * from './b{i + 1}';\n")
    _write(
        helpers / f"b{chain_len}.ts",
        "import { expect } from 'vitest';\n"
        "export function expectOk(result: { code: number }): void {\n"
        "  expect(result.code).toBe(0);\n"
        "}\n",
    )
    _write(helpers / "index.ts", "export * from './b0';\n")
    _write(
        project / "tests" / "deep.e2e.test.ts",
        "import { describe, it } from 'vitest';\n"
        'import { expectOk } from "./helpers";\n'
        "describe('deep', () => {\n"
        "  // codd: covers vb=VB-DEEP-01\n"
        "  it('too deep', () => {\n"
        "    const result = run();\n"
        "    expectOk(result);\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-DEEP-01" for v in report.violations)
    assert any("could not be resolved" in v.message for v in report.violations)


def test_gate_barrel_cycle_guard_terminates(tmp_path):
    """CYCLE GUARD: two barrels that re-export each other (`a → b → a`) and NEVER
    define the symbol must terminate (no infinite recursion) and resolve to a
    fail, not hang."""
    project = tmp_path
    _canonical(project, "| VB-CYC-01 | a |\n")
    helpers = project / "tests" / "helpers"
    _write(helpers / "a.ts", "export * from './b';\n")
    _write(helpers / "b.ts", "export * from './a';\n")  # cycle, no def anywhere
    _write(helpers / "index.ts", "export * from './a';\n")
    _write(
        project / "tests" / "cyc.e2e.test.ts",
        "import { describe, it } from 'vitest';\n"
        'import { expectOk } from "./helpers";\n'
        "describe('cyc', () => {\n"
        "  // codd: covers vb=VB-CYC-01\n"
        "  it('cyclic barrel', () => {\n"
        "    const result = run();\n"
        "    expectOk(result);\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-CYC-01" for v in report.violations)


def test_gate_passes_python_init_reexport(tmp_path):
    """Python parity: a test imports a helper from a package whose `__init__.py`
    re-exports it (`from .asserts import expect_ok`). Following the `__init__`
    re-export to `asserts.py` finds the real `assert` on its argument → PASS."""
    project = tmp_path
    _canonical(project, "| VB-PYRE-01 | a |\n")
    _write(
        project / "tests" / "helpers" / "asserts.py",
        "def expect_ok(result):\n    assert result.code == 0\n",
    )
    _write(
        project / "tests" / "helpers" / "__init__.py",
        "from .asserts import expect_ok\n",
    )
    _write(
        project / "tests" / "test_py_barrel.py",
        "from tests.helpers import expect_ok\n"
        "# codd: covers vb=VB-PYRE-01\n"
        "def test_runs():\n    result = run()\n    expect_ok(result)\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]


def test_gate_fails_python_init_reexport_constant_helper(tmp_path):
    """Python parity (anti-false-green): a `__init__` re-exporting a constant-only
    helper is reached but still fails — reach without a real argument anchor is
    not evidence."""
    project = tmp_path
    _canonical(project, "| VB-PYRE-02 | a |\n")
    _write(
        project / "tests" / "helpers" / "fake.py",
        "def verify_ok(result):\n    assert True\n",
    )
    _write(
        project / "tests" / "helpers" / "__init__.py",
        "from .fake import verify_ok\n",
    )
    _write(
        project / "tests" / "test_py_barrel_fake.py",
        "from tests.helpers import verify_ok\n"
        "# codd: covers vb=VB-PYRE-02\n"
        "def test_runs():\n    result = run()\n    verify_ok(result)\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-PYRE-02" for v in report.violations)


def test_gate_passes_python_init_alias_reexport(tmp_path):
    """Python parity: `from .asserts import expect_ok as expect_success` aliased
    re-export in `__init__` — follow must search the ORIGINAL name `expect_ok`."""
    project = tmp_path
    _canonical(project, "| VB-PYRE-03 | a |\n")
    _write(
        project / "tests" / "helpers" / "asserts.py",
        "def expect_ok(result):\n    assert result.code == 0\n",
    )
    _write(
        project / "tests" / "helpers" / "__init__.py",
        "from .asserts import expect_ok as expect_success\n",
    )
    _write(
        project / "tests" / "test_py_alias.py",
        "from tests.helpers import expect_success\n"
        "# codd: covers vb=VB-PYRE-03\n"
        "def test_runs():\n    result = run()\n    expect_success(result)\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]


def test_codex16_real_project_passes():
    """End-to-end against the actual codex16 greenfield output — the BARREL
    false-RED source. Its e2e tests import helpers from `tests/e2e/helpers`
    (a `index.ts` barrel of `export * from "./assertions"` etc.), and the real
    `expectSuccessResult` lives in `assertions.ts`. All `covers vb=` markers must
    be authentic: 0 violations, 0 degraded. Skips cleanly if the fixture is absent."""
    root = Path("/home/tono/codd-greenfield-tempconv-codex16")
    if not (root / "codd" / "codd.yaml").is_file():
        import pytest

        pytest.skip("codex16 fixture project not present")
    from codd.config import load_project_config

    config = load_project_config(root)
    profile = LayoutProfile(
        language="typescript",
        package_name="tempconv",
        source_root="src",
        package_root="src",
        test_root="tests",
    )
    report = build_authenticity_report(root, config=config, profile=profile)
    assert report.passed, [v.message for v in report.violations]
    assert report.degraded_paths == []


# ===========================================================================
# Round-4 precision: (1) helper-BODY extraction past a brace-bearing RETURN
# TYPE annotation, and (2) the argument anchor crediting a param-DERIVED local.
# These guard the noteapi-codex false-RED: a real helper `expectJsonError(...):
# Promise<{ error: string }>` whose body asserts on `body = await readJson(
# response)`. 2.34.0 mis-extracted the body as the return type's `{ error:
# string }` (no assertion ⇒ false-RED), and even with a correct body the anchor
# only saw a DIRECT param reference (not `body` derived from `response`). Both are
# fixed WITHOUT weakening anti-false-green: a constant-only helper still FAILS.
# ===========================================================================


from codd.vb_marker_authenticity import (  # noqa: E402 — grouped with the round-4 tests
    _body_references_params,
    _params_and_derived_locals,
    _ts_find_function_def,
    _PY_PRIMITIVE_ASSERT_RE,
    _TS_PRIMITIVE_ASSERT_RE,
)


# ── unit: _ts_find_function_def body extraction past brace-bearing return types ──


def test_ts_find_def_body_past_brace_bearing_return_type():
    """ROOT CAUSE #1: the body ``{`` must be found AFTER the return-type
    annotation, even when the annotation itself contains braces. The naive
    "first ``{`` after the params" picked the return type's brace (the noteapi
    false-RED). Each form below must yield a body that carries the real
    ``expect(...)`` (a primitive assertion), not the return-type's brace text."""
    forms = {
        # the exact noteapi shape: Promise<{ ... }>
        "promise_object": (
            "export async function f(a: Response, b: number): "
            "Promise<{ error: string }> {\n  expect(b).toBe(1);\n}"
        ),
        # bare object-type return literal: ': { ... }'
        "object_literal": "function g(a): { ok: boolean } {\n  expect(a.ok).toBe(true);\n}",
        # nested generic with an inner object: '<Array<{...}>>'
        "nested_generic": (
            "function h(a): Promise<Array<{ id: number }>> {\n  expect(a).toBe(1);\n}"
        ),
        # union of object literals
        "union_objects": (
            "function u(a): { ok: true } | { err: string } {\n  expect(a).toBe(1);\n}"
        ),
        # multi-line signature + brace-bearing return type (the literal noteapi form)
        "multiline": (
            "export async function w(\n  response: Response,\n  status: number\n"
            "): Promise<{ error: string }> {\n  expect(status).toBe(1);\n}"
        ),
        # plain (no return type) — regression
        "no_return_type": "function p(a) {\n  expect(a).toBe(1);\n}",
        # simple named return type — regression
        "void_return": "function v(a): void {\n  expect(a).toBe(1);\n}",
        # arrow with a brace-bearing return type
        "arrow_object_return": (
            "const q = (a): Promise<{ x: number }> => {\n  expect(a).toBe(1);\n}"
        ),
    }
    name_of = {
        "promise_object": "f",
        "object_literal": "g",
        "nested_generic": "h",
        "union_objects": "u",
        "multiline": "w",
        "no_return_type": "p",
        "void_return": "v",
        "arrow_object_return": "q",
    }
    for label, text in forms.items():
        found = _ts_find_function_def(text, name_of[label])
        assert found is not None, f"{label}: def not found"
        body, _params = found
        assert _TS_PRIMITIVE_ASSERT_RE.search(body), (
            f"{label}: body has no primitive assertion — likely the return type's "
            f"brace was mistaken for the body. body={body!r}"
        )


def test_ts_find_def_async_body_with_await_extracted():
    """The async helper body (with ``await`` lines) must be extracted whole —
    not truncated at the return type's brace. The noteapi helper has both an
    ``await`` and a brace-bearing return type."""
    text = (
        "export async function expectJsonError(\n"
        "  response: Response,\n"
        "  status: number,\n"
        "  exactError?: string\n"
        "): Promise<{ error: string }> {\n"
        "  await expectStatus(response, status);\n"
        "  const body = await readJson<{ error: string }>(response);\n"
        '  expect(typeof body.error).toBe("string");\n'
        "  if (exactError !== undefined) {\n"
        "    expect(body).toEqual({ error: exactError });\n"
        "  }\n"
        "  return body;\n"
        "}\n"
    )
    found = _ts_find_function_def(text, "expectJsonError")
    assert found is not None
    body, params = found
    assert params == ["response", "status", "exactError"]
    assert "await expectStatus(response, status)" in body
    assert "return body" in body  # the WHOLE body, through the last statement
    assert _TS_PRIMITIVE_ASSERT_RE.search(body)


def test_ts_expression_bodied_arrow_does_not_crash():
    """An expression-bodied arrow (no ``{`` block) must not mis-extract — the
    finder returns None rather than grabbing an unrelated brace (fails CLOSED)."""
    text = "const r = (a) => expect(a).toBe(1);\n"
    # No block body to extract; resolution simply finds no credible body (None).
    assert _ts_find_function_def(text, "r") is None


# ── unit: argument anchor over param-DERIVED locals (root cause #2) ──


def test_anchor_credits_param_derived_local():
    """ROOT CAUSE #2: an assertion on a local DERIVED from a param is anchored.
    ``const body = readJson(response); expect(body.error)`` flows ``response`` →
    ``body`` → the assertion, so it must count (the 2.34.0 anchor only saw a
    DIRECT param reference and wrongly rejected this)."""
    body = (
        "{\n"
        "  const body = await readJson(response);\n"
        '  expect(typeof body.error).toBe("string");\n'
        "}"
    )
    anchor = _params_and_derived_locals(body, {"response"})
    assert "body" in anchor and "response" in anchor
    assert _body_references_params(body, anchor, _TS_PRIMITIVE_ASSERT_RE) is True


def test_anchor_credits_two_hop_derived_local():
    """A 2-hop chain ``param → a → b`` then ``expect(b…)`` is anchored."""
    body = "{\n  const a = parse(input);\n  const b = a.payload;\n  expect(b.ok).toBe(true);\n}"
    anchor = _params_and_derived_locals(body, {"input"})
    assert {"input", "a", "b"} <= anchor
    assert _body_references_params(body, anchor, _TS_PRIMITIVE_ASSERT_RE) is True


def test_anchor_credits_destructured_derived_local():
    """A destructured bind ``const { error } = readJson(response)`` flows the
    param to ``error`` (object) / ``first`` (array)."""
    obj = '{\n  const { error } = await readJson(response);\n  expect(error).toBe("bad");\n}'
    anchor = _params_and_derived_locals(obj, {"response"})
    assert "error" in anchor
    assert _body_references_params(obj, anchor, _TS_PRIMITIVE_ASSERT_RE) is True
    arr = '{\n  const [first] = splitOf(input);\n  expect(first).toBe("x");\n}'
    anchor2 = _params_and_derived_locals(arr, {"input"})
    assert "first" in anchor2
    assert _body_references_params(arr, anchor2, _TS_PRIMITIVE_ASSERT_RE) is True


def test_anchor_python_param_derived_local():
    """ROOT CAUSE #2 (python): ``def check(resp): data = parse(resp); assert
    data.status == 200`` — ``data`` derives from the ``resp`` param, so anchored."""
    body = "def check(resp):\n    data = parse(resp)\n    assert data.status == 200\n"
    anchor = _params_and_derived_locals(body, {"resp"})
    assert {"resp", "data"} <= anchor
    assert _body_references_params(body, anchor, _PY_PRIMITIVE_ASSERT_RE) is True


def test_anchor_rejects_constant_only_even_with_unused_derived_local():
    """ANTI-FALSE-GREEN: a derived local existing in the body is NOT enough — the
    ASSERTION itself must reference a param/derived value. A constant-only assert
    (``expect(true).toBe(true)``) beside an unused derived local still FAILS."""
    body = "{\n  const x = result.foo;\n  log(x);\n  expect(true).toBe(true);\n}"
    anchor = _params_and_derived_locals(body, {"result"})
    assert "x" in anchor  # x IS derived…
    # …but the only assertion references neither result nor x → unanchored.
    assert _body_references_params(body, anchor, _TS_PRIMITIVE_ASSERT_RE) is False


def test_anchor_rejects_local_derived_from_constant():
    """ANTI-FALSE-GREEN: a local bound from a CONSTANT (not a param) is not a
    credible anchor — ``const k = 42; expect(k).toBe(42)`` proves nothing about
    the call's arguments and must FAIL."""
    body = "{\n  const k = 42;\n  expect(k).toBe(42);\n}"
    anchor = _params_and_derived_locals(body, {"result"})
    assert "k" not in anchor  # k flows from a constant, not a param
    assert _body_references_params(body, anchor, _TS_PRIMITIVE_ASSERT_RE) is False


# ── unit: RHS scanner ignores STRING LITERALS / COMMENTS / OBJECT KEYS ──
# ROOT CAUSE #3 (false-GREEN): the anchor harvested identifiers from a binding's
# RHS with a naive ``re.findall(IDENT, rhs)`` that swept up names inside string
# literals, comments, and object KEYS. ``const body = "response"`` then wrongly
# marked ``body`` param-derived, so a constant-only helper that merely *contains*
# that binding looked anchored ⇒ false-GREEN. The fix scans the RHS for genuine
# variable REFERENCES only. The four fixtures below are the mandated guard.


def test_anchor_credits_genuine_param_derived_local_repro_positive():
    """FIXTURE (a) — the repro POSITIVE must NOT regress: ``const body = await
    readJson(response); expect(body.error)…`` genuinely flows ``response`` →
    ``body`` (a real call argument), so ``body`` IS param-derived and the helper
    STAYS credited."""
    body = (
        "{\n"
        "  const body = await readJson(response);\n"
        '  expect(body.error).toBe("nope");\n'
        "}"
    )
    anchor = _params_and_derived_locals(body, {"response"})
    assert {"response", "body"} <= anchor
    assert _body_references_params(body, anchor, _TS_PRIMITIVE_ASSERT_RE) is True


def test_anchor_credits_different_genuine_param_derived_chains_positive():
    """FIXTURE (b) — out-of-repro, SAME bug class, DIFFERENT genuine chains stay
    credited: a python ``data = parse(resp); assert data.code == …`` and a
    distinct JS ``payload = decode(req).items`` both anchor on a real reference,
    proving the scanner credits references regardless of the specific names."""
    # python chain (the resolver handles python anchors too)
    py = "def check(resp):\n    data = parse(resp)\n    assert data.code == 200\n"
    py_anchor = _params_and_derived_locals(py, {"resp"})
    assert {"resp", "data"} <= py_anchor
    assert _body_references_params(py, py_anchor, _PY_PRIMITIVE_ASSERT_RE) is True
    # a DIFFERENT TS chain — value flows through a member access whose BASE is the
    # param (``decode(req)`` carries ``req``; ``.items`` is a property, not a ref)
    ts = (
        "{\n"
        "  const payload = decode(req).items;\n"
        "  expect(payload.length).toBe(3);\n"
        "}"
    )
    ts_anchor = _params_and_derived_locals(ts, {"req"})
    assert {"req", "payload"} <= ts_anchor
    assert _body_references_params(ts, ts_anchor, _TS_PRIMITIVE_ASSERT_RE) is True


def test_anchor_rejects_string_literal_and_object_key_spoofs_negative():
    """FIXTURE (c) — THE false-GREEN GUARD. Two spoofs must NOT be credited:

    1. STRING LITERAL: ``const body = "response"`` — ``response`` only appears
       inside a string, so ``body`` is NOT param-derived; a constant-only
       ``expect(true).toBe(true)`` beside it stays UNANCHORED ⇒ not credited.
    2. OBJECT KEY: ``const x = { response: 1 }`` — ``response`` is an object key,
       not a reference, so ``x`` is NOT derived.
    """
    spoof_string = '{\n  const body = "response";\n  expect(true).toBe(true);\n}'
    a1 = _params_and_derived_locals(spoof_string, {"response"})
    assert "body" not in a1  # the param name only lived inside a string literal
    assert _body_references_params(spoof_string, a1, _TS_PRIMITIVE_ASSERT_RE) is False

    spoof_objkey = "{\n  const x = { response: 1 };\n  expect(true).toBe(true);\n}"
    a2 = _params_and_derived_locals(spoof_objkey, {"response"})
    assert "x" not in a2  # the param name was an OBJECT KEY, not a reference
    assert _body_references_params(spoof_objkey, a2, _TS_PRIMITIVE_ASSERT_RE) is False


def test_anchor_credits_destructured_param_derived_local_false_red_guard():
    """FIXTURE (d) — FALSE-RED GUARD: a genuine destructured bind
    ``const { error } = await readJson(response); expect(error)…`` still flows the
    real ``response`` argument to ``error`` and STAYS credited (the scanner must
    not over-strip and wrongly fail a real anchor)."""
    body = (
        "{\n"
        "  const { error } = await readJson(response);\n"
        '  expect(error).toBe("title is required");\n'
        "}"
    )
    anchor = _params_and_derived_locals(body, {"response"})
    assert "error" in anchor
    assert _body_references_params(body, anchor, _TS_PRIMITIVE_ASSERT_RE) is True


# ── gate-level: the noteapi pattern PASSES, the constant-only sibling FAILS ──


_HTTP_HELPER_WITH_BRACE_RETURN = (
    "import { expect } from 'vitest';\n"
    "export async function expectStatus(response: Response, status: number): Promise<void> {\n"
    "  expect(response.status).toBe(status);\n"
    "}\n"
    "export async function readJson<T>(response: Response): Promise<T> {\n"
    "  return (await response.json()) as T;\n"
    "}\n"
    "export async function expectJsonError(\n"
    "  response: Response,\n"
    "  status: number,\n"
    "  exactError?: string\n"
    "): Promise<{ error: string }> {\n"
    "  await expectStatus(response, status);\n"
    "  const body = await readJson<{ error: string }>(response);\n"
    '  expect(typeof body.error).toBe("string");\n'
    "  if (exactError !== undefined) {\n"
    "    expect(body).toEqual({ error: exactError });\n"
    "  }\n"
    "  return body;\n"
    "}\n"
)


def test_gate_passes_helper_with_brace_bearing_return_type(tmp_path):
    """THE noteapi false-RED, gate level: a marker on a test delegating to
    ``expectJsonError`` (return type ``Promise<{ error: string }>``, asserting on a
    param-derived ``body``) must PASS. Before the fix this failed with
    'body contains NO assertion' for VB-31/32/46/47."""
    project = tmp_path
    _canonical(project, "| VB-ERR-01 | rejects with json error |\n")
    _write(project / "tests" / "e2e" / "helpers" / "http.ts", _HTTP_HELPER_WITH_BRACE_RETURN)
    _write(
        project / "tests" / "e2e" / "validation.spec.ts",
        "import { describe, it } from 'vitest';\n"
        "import { expectJsonError } from './helpers/http.js';\n"
        "describe('validation', () => {\n"
        "  // codd: covers vb=VB-ERR-01\n"
        "  it('rejects an empty title', async () => {\n"
        "    const response = await post('/notes', {});\n"
        "    await expectJsonError(response, 400, 'title is required');\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]
    assert report.degraded_paths == []


def test_gate_still_fails_constant_helper_with_brace_return_type(tmp_path):
    """ANTI-FALSE-GREEN: the body-extraction fix must NOT let a constant-only
    helper pass just because it now has a (brace-bearing) return type. A helper
    whose body is correctly extracted but asserts only a CONSTANT still FAILS."""
    project = tmp_path
    _canonical(project, "| VB-FAKE-RT | a |\n")
    _write(
        project / "tests" / "e2e" / "helpers" / "fake.ts",
        "import { expect } from 'vitest';\n"
        "export function expectFine(response: Response): { ok: boolean } {\n"
        "  expect(true).toBe(true);\n"  # constant only — references no param
        "  return { ok: true };\n"
        "}\n",
    )
    _write(
        project / "tests" / "e2e" / "fake.spec.ts",
        "import { describe, it } from 'vitest';\n"
        "import { expectFine } from './helpers/fake.js';\n"
        "describe('fake', () => {\n"
        "  // codd: covers vb=VB-FAKE-RT\n"
        "  it('pretends', async () => {\n"
        "    const response = await get('/x');\n"
        "    expectFine(response);\n"
        "  });\n"
        "});\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=TS_PROFILE
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-FAKE-RT" for v in report.violations)
    assert any("CONSTANT" in v.message for v in report.violations)


def test_gate_passes_python_param_derived_helper(tmp_path):
    """Gate level (python): a helper asserting on a param-DERIVED local, with a
    return-type annotation (``-> dict``), must PASS — the python anchor must also
    credit derived values."""
    project = tmp_path
    _canonical(project, "| VB-PY-DRV | a |\n")
    _write(project / "tests" / "helpers" / "__init__.py", "")
    _write(
        project / "tests" / "helpers" / "http.py",
        "def expect_json_error(resp) -> dict:\n"
        "    data = resp.json()\n"
        "    assert data['error'] is not None\n"
        "    return data\n",
    )
    _write(
        project / "tests" / "test_validation.py",
        "from tests.helpers.http import expect_json_error\n"
        "# codd: covers vb=VB-PY-DRV\n"
        "def test_rejects():\n"
        "    resp = client.post('/notes', json={})\n"
        "    expect_json_error(resp)\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]


# ── end-to-end against the real noteapi-codex project (the false-RED source) ──


def test_noteapi_codex_real_project_passes():
    """End-to-end against the actual noteapi-codex greenfield output — the source
    of the brace-bearing-return-type false-RED. Its e2e tests delegate to
    ``expectJsonError`` (return type ``Promise<{ error: string }>``). All
    ``covers vb=`` markers must be authentic: 0 violations, 0 degraded. Skips
    cleanly if the fixture project is absent."""
    root = Path("/home/tono/codd-greenfield-noteapi-codex")
    if not (root / "codd" / "codd.yaml").is_file():
        import pytest

        pytest.skip("noteapi-codex fixture project not present")
    from codd.config import load_project_config

    config = load_project_config(root)
    profile = LayoutProfile(
        language="typescript",
        package_name="noteapi",
        source_root="src",
        package_root="src",
        test_root="tests",
    )
    report = build_authenticity_report(root, config=config, profile=profile)
    assert report.passed, [v.message for v in report.violations]
    assert report.degraded_paths == []


# ---------------------------------------------------------------------------
# strict observability (contract authenticity.observable_in_supported_stack.v1)
# A marker-bearing file the adapter RECOGNIZES but parses NO test block out of is
# a false-green when silently degraded — but ONLY when the file is HARNESS-OWNED
# (CoDD generated it; it carries a `@generated-by: codd …` provenance header).
# strict mode honest-fails such a harness-owned file; a USER/CUSTOM recognized-
# extension file (no provenance header) DEGRADES — our block-parser's
# incompleteness (a Mocha variant, a decorated/wrapped test style) must not
# hard-RED a user's valid-but-unparsed-by-us test. An UNSUPPORTED file (no
# adapter) also degrades. Never a false-RED.
# ---------------------------------------------------------------------------


def test_strict_observability_flags_recognized_file_with_no_parseable_test(tmp_path):
    """KEYSTONE (fixture (a) — dogfood-repro positive): a HARNESS-OWNED .test.ts
    file (carries a `@generated-by: codd` header) the TS adapter RECOGNIZES but
    parses ZERO test blocks out of, bearing a live marker. Non-strict (default)
    degrades to a PASS (the pre-gate false-green); strict makes it an
    unobservable_test_structure VIOLATION.

    WHY UPDATED (was: a non-generated user file expecting hard-RED): per the
    harness-owned/user-custom design, strict observability now hard-fails ONLY a
    file CoDD itself generated — a user-authored recognized-extension file our
    parser cannot extract a block from must DEGRADE, not false-RED. So this
    keystone (which asserts the hard-RED) is made HARNESS-OWNED with a
    `@generated-by: codd` header; the false-RED path it used to assert is now
    covered by ``test_strict_observability_user_file_degrades_not_red`` (fixture
    (d)). Assertions are unchanged — only the fixture became harness-owned."""
    project = tmp_path
    _canonical(project, "| VB-1 | does a thing |\n")
    # A HARNESS-OWNED recognized test file (.test.ts): CoDD generated it (provenance
    # header present) but it has a live marker and NO it()/test() block.
    _write(
        project / "tests" / "empty.test.ts",
        "// @generated-by: codd implement\n"
        "// @generated-from: docs/design/x.md (x)\n"
        "// codd: covers vb=VB-1\nexport const helper = () => 42;\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    # Non-strict (back-compat): recognized-but-unparseable → degraded → PASSES.
    lax = build_authenticity_report(project, config=config, profile=TS_PROFILE)
    assert "tests/empty.test.ts" in lax.degraded_paths
    assert lax.passed is True
    # Strict + harness-owned: an unobservable coverage claim CoDD produced → hard.
    strict = build_authenticity_report(
        project, config=config, profile=TS_PROFILE, strict_observability=True
    )
    assert strict.passed is False
    assert "unobservable_test_structure" in {v.kind for v in strict.violations}
    assert any(v.vb_id == "VB-1" for v in strict.violations)
    # Not double-counted as degraded once it became a violation.
    assert "tests/empty.test.ts" not in strict.degraded_paths


def test_strict_observability_user_file_degrades_not_red(tmp_path):
    """FALSE-RED GUARD (fixture (d) — THE key one): a USER-authored recognized-
    extension file (NO `@generated-by` header) with a live `covers vb=` marker but
    ZERO parseable test blocks under strict_observability=True must NOT hard-RED —
    it DEGRADES. Our block-parser's incompleteness (e.g. a Mocha/decorated/wrapped
    test style we cannot extract from a valid .test.ts) must never false-RED a
    user's valid test. Stage-1 orphan checks still apply (the id here is declared,
    so no orphan)."""
    project = tmp_path
    _canonical(project, "| VB-1 | does a thing |\n")
    # USER-authored (no provenance header): a recognized .test.ts using a style our
    # block-parser does not extract a leaf it()/test() from (a framework wrapper).
    _write(
        project / "tests" / "user_custom.test.ts",
        "// codd: covers vb=VB-1\n"
        "import { suite } from './harness';\n"
        "suite('does a thing', { run: () => mustEqual(actual(), 1) });\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    strict = build_authenticity_report(
        project, config=config, profile=TS_PROFILE, strict_observability=True
    )
    # The key invariant: NOT a hard RED — it degrades and the report passes.
    assert strict.passed is True, [v.message for v in strict.violations]
    assert "tests/user_custom.test.ts" in strict.degraded_paths
    assert all(v.kind != "unobservable_test_structure" for v in strict.violations)


def test_strict_observability_harness_owned_spoof_is_red(tmp_path):
    """SPOOF NEGATIVE (fixture (c)): a HARNESS-OWNED generated file with a marker
    but an empty / no-assertion structure (0 parseable blocks) → RED. This is the
    false-green the contract catches: CoDD generated a marker-bearing file with no
    real, parseable test. The `@generated-from` line uses a python-ish path but the
    discriminator is purely the `@generated-by: codd` substring (language-agnostic).
    """
    project = tmp_path
    _canonical(project, "| VB-1 | does a thing |\n")
    # CoDD-generated (.test.ts) but only a stub export — no it()/test() at all.
    _write(
        project / "tests" / "spoof.test.ts",
        "// @generated-by: codd generate\n"
        "// codd: covers vb=VB-1\n"
        "export const noop = () => { /* TODO: write the real test */ };\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    strict = build_authenticity_report(
        project, config=config, profile=TS_PROFILE, strict_observability=True
    )
    assert strict.passed is False
    assert "unobservable_test_structure" in {v.kind for v in strict.violations}
    assert "tests/spoof.test.ts" not in strict.degraded_paths


def test_strict_observability_real_generated_test_is_credited(tmp_path):
    """OUT-OF-DOGFOOD POSITIVE (fixture (b)): a normal file with a real, parseable,
    asserting test is credited under strict mode — no regression. Harness-owned or
    not is irrelevant once a real block with an assertion is present; here it is a
    plain user test (no provenance header) to show a genuine test is never touched.
    """
    project = tmp_path
    _canonical(project, "| VB-1 | adds |\n")
    _write(
        project / "tests" / "real.test.ts",
        'import { it, expect } from "vitest";\n'
        "// codd: covers vb=VB-1\n"
        'it("adds", () => { const out = 1 + 1; expect(out).toBe(2); });\n',
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    strict = build_authenticity_report(
        project, config=config, profile=TS_PROFILE, strict_observability=True
    )
    assert strict.passed is True, [v.message for v in strict.violations]
    assert strict.degraded_paths == []


def test_strict_observability_still_degrades_unsupported_stack(tmp_path):
    """A file NO adapter handles still degrades in strict mode — strict must never
    false-RED an unsupported stack (only a RECOGNIZED-but-unparseable file fails)."""
    project = tmp_path
    _canonical(project, "| VB-1 | does a thing |\n")
    _write(project / "tests" / "test_x.py", "# codd: covers vb=VB-1\nx = 1\n")
    config = {"scan": {"test_dirs": ["tests/"]}}
    # profile=None → no adapter handles anything → unsupported → degrade (not fail).
    strict = build_authenticity_report(
        project, config=config, profile=None, strict_observability=True
    )
    assert "tests/test_x.py" in strict.degraded_paths
    assert all(v.kind != "unobservable_test_structure" for v in strict.violations)
    assert strict.passed is True


def test_strict_observability_does_not_affect_genuine_covering_test(tmp_path):
    """A real covering test (parseable block + assertion) passes in strict mode —
    strict only fires on the no-parseable-block case, never a genuine test."""
    project = tmp_path
    _canonical(project, "| VB-1 | adds |\n")
    _write(
        project / "tests" / "ok.test.ts",
        'import { it, expect } from "vitest";\n'
        "// codd: covers vb=VB-1\n"
        # A GENUINE observation: ``expect(add(1, 1)).toBe(2)`` references the SUT
        # call ``add`` (a non-ignored name), so it is real evidence. (A constant
        # ``expect(1 + 1).toBe(2)`` would now be ``constant_direct`` — correctly,
        # since 1 + 1 is compile-time-constant and proves no behavior; this test
        # is about the strict_observability flag, not constant-direct, so it uses
        # a genuinely-covering assertion.)
        'it("adds", () => { expect(add(1, 1)).toBe(2); });\n',
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    strict = build_authenticity_report(
        project, config=config, profile=TS_PROFILE, strict_observability=True
    )
    assert strict.passed is True, [v.message for v in strict.violations]
    assert strict.degraded_paths == []


# ---------------------------------------------------------------------------
# assertion-helper import resolution via the ast.ImportFrom rewrite
# (regression: PC-assertion-helper-package-barrel-falsered) — a covers-marker on a
# test that asserts via an `assert_*` helper imported through a MULTI-LINE package
# barrel must be credited. The pre-fix regex truncated `from pkg.helpers import (`
# to `(` and wrongly reported no_assertion, blocking real Python greenfield.
# ---------------------------------------------------------------------------


def _pkg_barrel_helper_project(tmp_path, *, helper_body: str, importer: str):
    """A src-layout-ish project whose e2e test imports an `assert_ok` helper through
    a tests/e2e/helpers package barrel (__init__ re-exports from .asserts)."""
    project = tmp_path
    _canonical(project, "| VB-1 | does a thing |\n")
    _write(project / "tests" / "__init__.py", "")
    _write(project / "tests" / "e2e" / "__init__.py", "")
    _write(project / "tests" / "e2e" / "helpers" / "__init__.py", "from .asserts import (\n    assert_ok,\n)\n")
    _write(project / "tests" / "e2e" / "helpers" / "asserts.py", helper_body)
    _write(
        project / "tests" / "e2e" / "test_cli.py",
        importer + "\n\n# codd: covers vb=VB-1\ndef test_cli_ok():\n    result = 0\n    assert_ok(result)\n",
    )
    return project


def test_py_multiline_barrel_helper_import_is_credited(tmp_path):
    """KEYSTONE: a covers-marker whose test asserts via an `assert_ok` helper imported
    through a MULTI-LINE package barrel resolves + is credited (was a false-RED)."""
    project = _pkg_barrel_helper_project(
        tmp_path,
        helper_body="def assert_ok(result):\n    assert result == 0\n",  # real assert on a param
        importer="from tests.e2e.helpers import (\n    assert_ok,\n)",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert report.passed is True, [(v.kind, v.message) for v in report.violations]


def test_py_multiline_barrel_nonassert_helper_still_fails(tmp_path):
    """The SAME multi-line barrel shape, but the helper does NOT assert → no_assertion.
    The fix widens import RESOLUTION, never the assertion PROOF — pins no false-green."""
    project = _pkg_barrel_helper_project(
        tmp_path,
        helper_body="def assert_ok(result):\n    return None\n",  # NO assertion
        importer="from tests.e2e.helpers import (\n    assert_ok,\n)",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert report.passed is False
    assert any(v.kind == "no_assertion" for v in report.violations), [v.kind for v in report.violations]


def test_py_backslash_continuation_import_resolves(tmp_path):
    """A backslash-continuation import (which a names-regex fix would have MISSED) is
    parsed by the ast rewrite → the helper resolves + is credited."""
    project = _pkg_barrel_helper_project(
        tmp_path,
        helper_body="def assert_ok(result):\n    assert result == 0\n",
        importer="from tests.e2e.helpers import assert_ok, \\\n    assert_ok as _alias",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert report.passed is True, [(v.kind, v.message) for v in report.violations]


# ---------------------------------------------------------------------------
# Python marker-authenticity helper-resolution gaps
# (regression: PC-py-marker-authenticity-helper-resolution-falsered) — three
# false-REDs a real Codex Python web greenfield hit at the gate. The fix widens
# helper RESOLUTION (absolute barrel re-export, same-file private helper) and
# marker ATTACHMENT (multi-line decorator) only; the PROVABLY-asserts-only proof
# is unchanged, so the anti-false-green fixtures below stay RED.
# ---------------------------------------------------------------------------


def _abs_barrel_project(tmp_path, *, helper_body: str, barrel_import: str):
    """A project whose e2e test imports `assert_ok` from a tests/e2e/helpers
    package barrel that re-exports it via an ABSOLUTE import (``barrel_import``)."""
    project = tmp_path
    _canonical(project, "| VB-1 | does a thing |\n")
    _write(project / "tests" / "__init__.py", "")
    _write(project / "tests" / "helpers.py", helper_body)
    _write(project / "tests" / "e2e" / "__init__.py", "")
    _write(project / "tests" / "e2e" / "helpers" / "__init__.py", barrel_import + "\n")
    _write(
        project / "tests" / "e2e" / "test_cli.py",
        "from tests.e2e.helpers import assert_ok\n\n# codd: covers vb=VB-1\n"
        "def test_cli_ok():\n    result = 0\n    assert_ok(result)\n",
    )
    return project


def test_py_absolute_barrel_reexport_helper_is_credited(tmp_path):
    """Gap 1: a barrel that re-exports an asserting helper via an ABSOLUTE import
    (`from tests.helpers import assert_ok`) resolves to the same-repo def → credited."""
    project = _abs_barrel_project(
        tmp_path,
        helper_body="def assert_ok(result):\n    assert result == 0\n",
        barrel_import="from tests.helpers import assert_ok",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert report.passed is True, [(v.kind, v.message) for v in report.violations]


def test_py_absolute_barrel_nonassert_helper_stays_red(tmp_path):
    """Anti-false-green: following the ABSOLUTE re-export does NOT credit a helper
    whose body has no primitive assertion (resolution widened, proof unchanged)."""
    project = _abs_barrel_project(
        tmp_path,
        helper_body="def assert_ok(result):\n    return None\n",
        barrel_import="from tests.helpers import assert_ok",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert report.passed is False
    assert any(v.kind == "no_assertion" for v in report.violations), [v.kind for v in report.violations]


def test_py_thirdparty_absolute_reexport_not_followed(tmp_path):
    """Anti-false-green: an ABSOLUTE re-export from a NON-project module (no project
    file, e.g. `from requests import ...`) stays unresolved and cannot credit the
    marker — only same-repo modules are followed."""
    project = _abs_barrel_project(
        tmp_path,
        helper_body="def unused():\n    assert True\n",
        barrel_import="from requests import assert_ok",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert report.passed is False
    assert any(v.kind == "no_assertion" for v in report.violations), [v.kind for v in report.violations]


def test_py_same_file_underscore_helper_is_credited(tmp_path):
    """Gap 2: a marker on a test that asserts via a same-file PRIVATE helper
    (`_assert_error`, leading underscore) whose body asserts on its argument is
    credited — the candidate filter previously dropped leading-underscore names."""
    project = tmp_path
    _canonical(project, "| VB-LOCAL-01 | a |\n")
    _write(
        project / "tests" / "test_local.py",
        "def _assert_error(response, status):\n    assert response.status_code == status\n\n"
        "# codd: covers vb=VB-LOCAL-01\n"
        "def test_rejects():\n    resp = call()\n    _assert_error(resp, 400)\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert report.passed is True, [(v.kind, v.message) for v in report.violations]


def test_py_nested_helper_def_is_not_credited(tmp_path):
    """Anti-false-green (Gap 2 hardening): a helper def NESTED inside another
    function is not an importable/callable binding (a runtime NameError), so its
    assertion must not credit the marker — only top-level defs are followed."""
    project = tmp_path
    _canonical(project, "| VB-NEST-01 | a |\n")
    _write(
        project / "tests" / "test_nested.py",
        "def outer():\n    def _assert_error(response, status):\n"
        "        assert response.status_code == status\n\n"
        "# codd: covers vb=VB-NEST-01\n"
        "def test_error():\n    resp = call()\n    _assert_error(resp, 400)\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert report.passed is False
    assert any(v.kind == "no_assertion" for v in report.violations), [v.kind for v in report.violations]


def test_py_marker_above_multiline_decorator_attaches(tmp_path):
    """Gap 3: a marker placed above a MULTI-LINE @pytest.mark.parametrize attaches
    to the decorated test (was reported 'not attached to a test block')."""
    project = tmp_path
    _canonical(project, "| VB-DECO-01 | a |\n")
    _write(
        project / "tests" / "test_deco.py",
        "import pytest\n\n# codd: covers vb=VB-DECO-01\n"
        "@pytest.mark.parametrize(\n    ('status', 'code'),\n    [(400, 'bad_request')],\n)\n"
        "def test_error(status, code):\n    resp = call()\n    assert resp.status_code == status\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert report.passed is True, [(v.kind, v.message) for v in report.violations]


# ---------------------------------------------------------------------------
# v2.51.0 — multi-line def signature body extraction (no_assertion FALSE-RED).
# A multi-line signature collapsed the line-scanner body to the parameter-
# annotation lines (the closing `) -> None:` was misread as the body dedent),
# yielding a spurious no_assertion on a test that genuinely asserts. AST body
# extraction fixes it without loosening has_assertion (raw primitive check).
# ---------------------------------------------------------------------------


def test_python_multiline_signature_body_is_fully_extracted():
    # (A) dogfood-repro (LangGraph Codex): multi-line signature + nested def +
    # with-block + a param-derived (capsys) assertion. Body must be complete.
    text = (
        "import pytest\n"
        "from app import cli\n"
        "\n"
        "def test_missing_text_exits(\n"
        "    monkeypatch: pytest.MonkeyPatch,\n"
        "    capsys: pytest.CaptureFixture[str],\n"
        ") -> None:\n"
        "    def fail_if_called(*a: object, **k: object) -> int:\n"
        "        raise AssertionError('should not run')\n"
        "    monkeypatch.setattr(cli, 'run_command', fail_if_called)\n"
        "    with pytest.raises(SystemExit) as exc_info:\n"
        "        cli.main(['run'])\n"
        "    captured = capsys.readouterr()\n"
        "    assert exc_info.value.code != 0\n"
        "    assert captured.out == ''\n"
        "    assert '--text' in captured.err\n"
    )
    b = {x.label: x for x in PythonTestBlockProfile().parse_test_blocks(text)}[
        "test_missing_text_exits"
    ]
    assert b.has_assertion is True
    assert "assert captured.out == ''" in b.body_text
    # (D) root-cause guards: signature annotations must NOT leak into the body,
    # and the nested def MUST be inside the body (not a premature boundary).
    assert "monkeypatch: pytest.MonkeyPatch" not in b.body_text
    assert "capsys: pytest.CaptureFixture[str]" not in b.body_text
    assert "def fail_if_called" in b.body_text


def test_python_multiline_signature_simple_positive():
    # (B) different structure: multi-line signature, NO nested def, assertion
    # after a with-block. Still credited.
    text = (
        "import pytest\n"
        "from app import cli\n"
        "\n"
        "def test_usage_on_missing_arg(\n"
        "    capsys: pytest.CaptureFixture[str],\n"
        ") -> None:\n"
        "    with pytest.raises(SystemExit):\n"
        "        cli.main(['run'])\n"
        "    captured = capsys.readouterr()\n"
        "    assert 'usage' in captured.err\n"
    )
    b = {x.label: x for x in PythonTestBlockProfile().parse_test_blocks(text)}[
        "test_usage_on_missing_arg"
    ]
    assert b.has_assertion is True


def test_python_multiline_signature_empty_body_stays_no_assertion():
    # Negative guard: the AST fix must NOT make a genuinely empty multi-line-sig
    # test look asserted (no new false-GREEN).
    text = (
        "def test_empty_multiline(\n"
        "    a: int,\n"
        "    b: int,\n"
        ") -> None:\n"
        "    pass\n"
    )
    b = {x.label: x for x in PythonTestBlockProfile().parse_test_blocks(text)}[
        "test_empty_multiline"
    ]
    assert b.has_assertion is False


def test_python_singleline_signature_still_parsed():
    # Regression guard: single-line signatures (the common case) keep working.
    text = "def test_single():\n    result = compute(2)\n    assert result == 4\n"
    b = {x.label: x for x in PythonTestBlockProfile().parse_test_blocks(text)}[
        "test_single"
    ]
    assert b.has_assertion is True


# ---------------------------------------------------------------------------
# Stage 3 — library-only direct assertion (contract direct.library_only_reference.v1)
# A marker-attached test whose direct assertion observes ONLY a library (stdlib /
# confirmed third-party / builtin) and never the SUT proves the LIBRARY, not the VB
# — a false-GREEN. The fix must REJECT those while NEVER false-RED'ing a real SUT
# observation (a first-party import, a local, a fixture, or an unknown reference).
# ---------------------------------------------------------------------------


def _scan_cfg() -> dict:
    return {"scan": {"test_dirs": ["tests/"]}}


def _report(project) -> object:
    return build_authenticity_report(project, config=_scan_cfg(), profile=PY_PROFILE)


@pytest.mark.parametrize(
    "name, body",
    [
        ("stdlib_math", "import math\n# codd: covers vb=VB-01\ndef test_x():\n    assert math.sqrt(4) == 2.0\n"),
        ("stdlib_os", "import os\n# codd: covers vb=VB-01\ndef test_x():\n    assert os.path.join('a', 'b') == 'a/b'\n"),
        ("builtin_sorted", "# codd: covers vb=VB-01\ndef test_x():\n    assert sorted([3, 1, 2]) == [1, 2, 3]\n"),
    ],
)
def test_library_only_direct_is_rejected(tmp_path, name, body):
    """stdlib / builtin-only assertions never reference the SUT → library_only_direct."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(tmp_path / "tests" / "test_x.py", body)
    report = _report(tmp_path)
    assert not report.passed, name
    assert any(
        v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations
    ), [(v.kind, v.message) for v in report.violations]


def test_third_party_only_direct_rejected_with_manifest(tmp_path):
    """A third-party import is library-only — but only POSITIVELY when a manifest
    confirms it (an unconfirmable dep stays UNKNOWN ⇒ credit, by design)."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "app"\nversion = "0.0.0"\ndependencies = ["requests>=2.0"]\n',
    )
    _write(
        tmp_path / "tests" / "test_x.py",
        "import requests\n# codd: covers vb=VB-01\ndef test_x():\n    assert requests.codes.ok == 200\n",
    )
    report = _report(tmp_path)
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)


def test_third_party_only_without_manifest_fails_open(tmp_path):
    """No manifest ⇒ `requests` origin is unconfirmable ⇒ UNKNOWN ⇒ credit (fail-OPEN).

    This is the deliberate false-RED-avoidance trade-off: a residual false-GREEN is
    accepted rather than risk rejecting a real SUT observation we cannot classify."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(
        tmp_path / "tests" / "test_x.py",
        "import requests\n# codd: covers vb=VB-01\ndef test_x():\n    assert requests.codes.ok == 200\n",
    )
    assert _report(tmp_path).passed


@pytest.mark.parametrize(
    "name, body, manifest",
    [
        (
            "sut_direct_call",
            "from app.calc import compute\n# codd: covers vb=VB-01\ndef test_x():\n    assert compute(2, 3) == 5\n",
            False,
        ),
        (
            "sut_result_via_local",
            "from app.api import client\n# codd: covers vb=VB-01\n"
            "def test_x():\n    r = client.get('/health')\n    assert r.status_code == 200\n",
            False,
        ),
        (
            "sut_wrapped_by_stdlib",
            "import json\nfrom app.api import sut_payload\n# codd: covers vb=VB-01\n"
            "def test_x():\n    assert json.loads(sut_payload()) == {'x': 1}\n",
            False,
        ),
        (
            "sut_local_plus_stdlib",
            "import json\nfrom app.api import call\n# codd: covers vb=VB-01\n"
            "def test_x():\n    parsed = json.loads(call())\n    assert parsed['x'] == 1\n",
            False,
        ),
        (
            "first_party_alias",
            "from app.calc import compute as c\n# codd: covers vb=VB-01\ndef test_x():\n    assert c(2, 3) == 5\n",
            False,
        ),
        (
            "star_first_party_fail_open",
            "from app import *\n# codd: covers vb=VB-01\ndef test_x():\n    assert compute(2, 3) == 5\n",
            False,
        ),
        (
            "unknown_absolute_import_fail_open",
            "from unknown_runtime_package import compute\n# codd: covers vb=VB-01\n"
            "def test_x():\n    assert compute(2, 3) == 5\n",
            False,
        ),
        (
            "builtin_shadowed_by_first_party",
            "from app.sorting import sorted\n# codd: covers vb=VB-01\ndef test_x():\n    assert sorted([3, 1, 2]) == [1, 2, 3]\n",
            False,
        ),
        (
            "builtin_with_sut_arg",
            "from app.data import sut_list\n# codd: covers vb=VB-01\ndef test_x():\n    assert len(sut_list()) == 3\n",
            False,
        ),
        (
            "fixture_param_credited",
            "# codd: covers vb=VB-01\ndef test_x(snapshot):\n    assert snapshot.matches('ok')\n",
            False,
        ),
    ],
)
def test_real_sut_observation_is_never_false_red(tmp_path, name, body, manifest):
    """Every legitimate SUT-observation shape MUST keep passing (no false-RED)."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    if manifest:
        _write(
            tmp_path / "pyproject.toml",
            '[project]\nname = "app"\nversion = "0.0.0"\ndependencies = ["requests>=2.0"]\n',
        )
    _write(tmp_path / "tests" / "test_x.py", body)
    report = _report(tmp_path)
    assert report.passed, (name, [(v.kind, v.message) for v in report.violations])


def test_library_only_direct_falls_back_to_credible_helper(tmp_path):
    """A library-only direct assertion does NOT RED when the SAME block also calls a
    credible same-repo helper (library_only_direct flows into the SAME helper-resolver
    fallback that constant_direct does — the fallback is preserved)."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(tmp_path / "tests" / "helpers" / "__init__.py", "")
    _write(
        tmp_path / "tests" / "helpers" / "asserts.py",
        "def expect_ok(result):\n    assert result.code == 0\n",
    )
    _write(
        tmp_path / "tests" / "test_x.py",
        "import math\n"
        "from tests.helpers.asserts import expect_ok\n"
        "# codd: covers vb=VB-01\n"
        "def test_x():\n    assert math.sqrt(4) == 2.0\n    result = run()\n    expect_ok(result)\n",
    )
    # The direct assert is library-only (math.sqrt), but the block also delegates to
    # a helper whose body asserts on its argument — credit via the helper path.
    assert _report(tmp_path).passed


# ---------------------------------------------------------------------------
# `raise AssertionError(...)` is a primitive assertion (the explicit form of
# `assert`; pytest.fail is already recognized). Recognizing it fixes a false-RED on
# valid raise-based tests/helpers WITHOUT crediting a no-op constant raise or an
# arbitrary `raise <Exception>` (false-GREEN containment).
# ---------------------------------------------------------------------------


def test_inline_raise_assertion_error_referencing_sut_passes(tmp_path):
    """A conditional `raise AssertionError(f"... {x}")` over SUT-derived values is a
    real assertion (the shape generated by AST-introspection invariant tests)."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(
        tmp_path / "tests" / "test_x.py",
        "from app.rules import is_forbidden\n"
        "# codd: covers vb=VB-01\n"
        "def test_x():\n"
        "    name = 'requests'\n"
        "    if is_forbidden(name):\n"
        "        raise AssertionError(f'forbidden import: {name}')\n",
    )
    assert _report(tmp_path).passed, [(v.kind, v.message) for v in _report(tmp_path).violations]


def test_helper_raising_assertion_error_on_its_arg_passes(tmp_path):
    """A helper that `raise AssertionError(...)` based on its argument (assert_failure
    shape) is credible (helper arg-anchor over a raise-based primitive)."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(tmp_path / "tests" / "helpers" / "__init__.py", "")
    _write(
        tmp_path / "tests" / "helpers" / "asserts.py",
        "def assert_failure(process):\n"
        "    if process.returncode == 0:\n"
        "        raise AssertionError('command did not fail', {'err': process.stderr})\n",
    )
    _write(
        tmp_path / "tests" / "test_x.py",
        "from tests.helpers.asserts import assert_failure\n"
        "from app.run import run_cmd\n"
        "# codd: covers vb=VB-01\n"
        "def test_x():\n    assert_failure(run_cmd('bad-input'))\n",
    )
    assert _report(tmp_path).passed, [(v.kind, v.message) for v in _report(tmp_path).violations]


@pytest.mark.parametrize(
    "name, body",
    [
        # unconditional constant raise — no observation → constant_direct.
        ("unconditional_constant_raise", "# codd: covers vb=VB-01\ndef test_x():\n    raise AssertionError('x')\n"),
        # arbitrary exception — NOT an assertion (only AssertionError counts).
        ("arbitrary_value_error_raise", "# codd: covers vb=VB-01\ndef test_x():\n    raise ValueError('x')\n"),
        # bare re-raise — not an assertion.
        (
            "bare_reraise",
            "# codd: covers vb=VB-01\ndef test_x():\n    try:\n        pass\n    except Exception:\n        raise\n",
        ),
    ],
)
def test_non_credible_raise_is_rejected(tmp_path, name, body):
    """A constant / arbitrary / re-raise must NOT be credited as an assertion."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(tmp_path / "tests" / "test_x.py", body)
    report = _report(tmp_path)
    assert not report.passed, name
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)


def test_helper_constant_raise_is_rejected(tmp_path):
    """A helper that raises a CONSTANT AssertionError (never references its arg) → constant_helper."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(tmp_path / "tests" / "helpers" / "__init__.py", "")
    _write(
        tmp_path / "tests" / "helpers" / "fake.py",
        "def assert_fake(result):\n    raise AssertionError('x')\n",
    )
    _write(
        tmp_path / "tests" / "test_x.py",
        "from tests.helpers.fake import assert_fake\n"
        "from app.run import run_cmd\n"
        "# codd: covers vb=VB-01\n"
        "def test_x():\n    assert_fake(run_cmd('a'))\n",
    )
    report = _report(tmp_path)
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)


def test_raise_assertion_error_in_string_does_not_count(tmp_path):
    """AST-first has_assertion: a `raise AssertionError(...)` inside a STRING is not a
    primitive — the body has no real assertion → rejected (no string false-GREEN)."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(
        tmp_path / "tests" / "test_x.py",
        "from app.run import run_sut\n"
        "# codd: covers vb=VB-01\n"
        "def test_x():\n"
        "    text = 'raise AssertionError(result)'\n"
        "    result = run_sut()\n",
    )
    report = _report(tmp_path)
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)


def test_is_py_assertion_error_raise_unit():
    """Parser unit: only `raise AssertionError[(...)]` is recognized, not other raises."""
    import ast as _ast

    from codd.vb_marker_authenticity import _is_py_assertion_error_raise

    def _raise_node(src):
        return next(n for n in _ast.walk(_ast.parse(src)) if isinstance(n, _ast.Raise))

    assert _is_py_assertion_error_raise(_raise_node("raise AssertionError('x')"))
    assert _is_py_assertion_error_raise(_raise_node("raise AssertionError"))
    assert not _is_py_assertion_error_raise(_raise_node("raise ValueError('x')"))
    assert not _is_py_assertion_error_raise(_raise_node("try:\n    pass\nexcept Exception:\n    raise"))


# ---------------------------------------------------------------------------
# TS/JS library-only direct assertion (contract direct.library_only_reference.v1,
# cross-language). A vitest test whose assertion references ONLY a confirmed-external
# dependency (in package.json deps) proves the LIBRARY, not the SUT → reject. But a
# relative/alias/workspace/local/unknown reference must NEVER false-RED (fail-open).
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402


def _ts_project(tmp_path: Path, test_body: str, *, package_json: dict | None = None) -> Path:
    _canonical(tmp_path, "| VB-01 | demo |\n")
    pkg = package_json if package_json is not None else {
        "name": "app",
        "version": "0.0.0",
        "dependencies": {"lodash": "^4.0.0", "@scope/pkg": "^1.0.0"},
    }
    _write(tmp_path / "package.json", _json.dumps(pkg))
    _write(tmp_path / "tests" / "x.test.ts", test_body)
    return tmp_path


@pytest.mark.parametrize(
    "name, body, should_pass",
    [
        # --- legitimate SUT observations / fail-open: MUST PASS (no false-RED) ---
        ("relative_first_party", "import { compute } from '../src/calc';\n// codd: covers vb=VB-01\nit('x', () => { expect(compute(2,3)).toBe(5); });\n", True),
        ("alias_not_in_deps", "import { compute } from '@/calc';\n// codd: covers vb=VB-01\nit('x', () => { expect(compute(2,3)).toBe(5); });\n", True),
        ("bare_not_in_deps", "import { compute } from 'mystuff';\n// codd: covers vb=VB-01\nit('x', () => { expect(compute(2,3)).toBe(5); });\n", True),
        ("local_result", "import { compute } from '../src/calc';\n// codd: covers vb=VB-01\nit('x', () => { const r = compute(); expect(r.value).toBe(1); });\n", True),
        ("local_shadow_of_library", "import lodash from 'lodash';\n// codd: covers vb=VB-01\nit('x', () => { const lodash = compute(); expect(lodash.value).toBe(1); });\n", True),
        ("library_plus_sut", "import lodash from 'lodash';\nimport { compute } from '../src/calc';\n// codd: covers vb=VB-01\nit('x', () => { expect(lodash.isEqual(compute(), 1)).toBe(true); });\n", True),
        # --- library-only proofs: MUST REJECT (the seam) ---
        ("bare_dep_default", "import lodash from 'lodash';\n// codd: covers vb=VB-01\nit('x', () => { expect(lodash.add(1,2)).toBe(3); });\n", False),
        ("bare_dep_named", "import { add } from 'lodash';\n// codd: covers vb=VB-01\nit('x', () => { expect(add(1,2)).toBe(3); });\n", False),
        ("scoped_dep", "import { x } from '@scope/pkg';\n// codd: covers vb=VB-01\nit('x', () => { expect(x.v).toBe(1); });\n", False),
        ("constant_only", "// codd: covers vb=VB-01\nit('x', () => { expect(2 + 2).toBe(4); });\n", False),
    ],
)
def test_ts_library_only_direct(tmp_path, name, body, should_pass):
    project = _ts_project(tmp_path, body)
    report = build_authenticity_report(project, config=_scan_cfg(), profile=TS_PROFILE)
    if should_pass:
        assert report.passed, (name, [(v.kind, v.message) for v in report.violations])
    else:
        assert not report.passed, name
        assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)


def test_ts_workspace_protocol_dep_is_not_library(tmp_path):
    """A workspace/local-protocol dependency (``workspace:*``) is first-party, not a
    library — it must NOT false-RED even though it appears in package.json deps."""
    pkg = {"name": "app", "version": "0.0.0", "dependencies": {"@org/shared": "workspace:*"}}
    project = _ts_project(
        tmp_path,
        "import { compute } from '@org/shared';\n// codd: covers vb=VB-01\n"
        "it('x', () => { expect(compute()).toBe(1); });\n",
        package_json=pkg,
    )
    assert build_authenticity_report(project, config=_scan_cfg(), profile=TS_PROFILE).passed


def test_ts_library_only_unit_classifier():
    """Unit: _classify_ts_name / _ts_package_name boundaries."""
    from codd.vb_marker_authenticity import _TsOriginContext, _classify_ts_name, _ts_package_name

    assert _ts_package_name("lodash/fp") == "lodash"
    assert _ts_package_name("@scope/pkg/sub") == "@scope/pkg"
    assert _ts_package_name("../src/x") == ""
    ctx = _TsOriginContext(
        imports={"lodash": "lodash", "compute": "../src/calc", "aliased": "@/calc"},
        local_names=frozenset({"r"}),
        external_deps=frozenset({"lodash"}),
    )
    assert _classify_ts_name("lodash", ctx) == "library"  # confirmed external
    assert _classify_ts_name("compute", ctx) == "credit"  # relative first-party
    assert _classify_ts_name("aliased", ctx) == "credit"  # alias not in deps -> unknown
    assert _classify_ts_name("r", ctx) == "credit"  # local
    assert _classify_ts_name("unknownName", ctx) == "credit"  # untracked -> unknown


# ---------------------------------------------------------------------------
# CROSS-LANGUAGE PARITY: the TS/JS path must credit the TS-equivalents of the
# recent PYTHON fixes (throw = raise-AssertionError v2.56; expect(()=>).toThrow /
# assert.throws = with self.assertRaises v2.61; an expect-asserting helper on its
# data arg = testcase.assert* helper v2.62) AND still REJECT a constant-only helper.
# Empirically confirmed at parity (PARITY HOLDS) — these lock it so a future
# refactor cannot silently weaken TS while the Python tests stay green.
# ---------------------------------------------------------------------------


def test_ts_throw_on_sut_condition_is_credited(tmp_path):
    """``throw new Error(...)`` guarded by a SUT-derived condition proves the SUT
    (the TS analogue of Python ``raise AssertionError``)."""
    project = _ts_project(
        tmp_path,
        "import { parse } from '../src/calc';\n"
        "// codd: covers vb=VB-01\n"
        "it('x', () => { if (parse('2+3') !== 5) throw new Error('bad'); });\n",
    )
    report = build_authenticity_report(project, config=_scan_cfg(), profile=TS_PROFILE)
    assert report.passed, [(v.kind, v.message) for v in report.violations]


def test_ts_expect_tothrow_on_sut_is_credited(tmp_path):
    """``expect(() => sut()).toThrow()`` proves the SUT raises (the TS analogue of
    ``with self.assertRaises``)."""
    project = _ts_project(
        tmp_path,
        "import { parse } from '../src/calc';\n"
        "// codd: covers vb=VB-01\n"
        "it('x', () => { expect(() => parse('bad')).toThrow(); });\n",
    )
    report = build_authenticity_report(project, config=_scan_cfg(), profile=TS_PROFILE)
    assert report.passed, [(v.kind, v.message) for v in report.violations]


def test_ts_assert_throws_on_sut_is_credited(tmp_path):
    """chai/node ``assert.throws(() => sut())`` proves the SUT raises."""
    project = _ts_project(
        tmp_path,
        "import { strict as assert } from 'node:assert';\n"
        "import { parse } from '../src/calc';\n"
        "// codd: covers vb=VB-01\n"
        "it('x', () => { assert.throws(() => parse('bad')); });\n",
    )
    report = build_authenticity_report(project, config=_scan_cfg(), profile=TS_PROFILE)
    assert report.passed, [(v.kind, v.message) for v in report.violations]


def test_ts_expect_helper_asserting_on_its_arg_is_credited(tmp_path):
    """A helper ``function assertTokens(actual, expected){ expect(actual).toEqual(expected) }``
    asserting on its data arg is credible (TS analogue of the testcase.assert* helper)."""
    project = _ts_project(
        tmp_path,
        "import { tokenize } from '../src/calc';\n"
        "function assertTokens(actual: number[], expected: number[]) {\n"
        "  expect(actual).toEqual(expected);\n"
        "}\n"
        "// codd: covers vb=VB-01\n"
        "it('x', () => { assertTokens(tokenize('1 2'), [1, 2]); });\n",
    )
    report = build_authenticity_report(project, config=_scan_cfg(), profile=TS_PROFILE)
    assert report.passed, [(v.kind, v.message) for v in report.violations]


def test_ts_constant_only_helper_is_rejected(tmp_path):
    """A helper that asserts only on a constant (``expect(true).toBe(true)``) is a
    no-op — crediting the delegating test would be a false-GREEN."""
    project = _ts_project(
        tmp_path,
        "function check() { expect(true).toBe(true); }\n"
        "// codd: covers vb=VB-01\n"
        "it('x', () => { check(); });\n",
    )
    report = build_authenticity_report(project, config=_scan_cfg(), profile=TS_PROFILE)
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)


# ---------------------------------------------------------------------------
# unittest `with` context-manager assertions (with self.assertRaises / assertWarns /
# assertLogs) are primitive assertions — symmetric with `with pytest.raises`. v2.56's
# AST-first has_assertion regressed these (the old regex matched `self.assertRaises(`);
# the With-handler must recognize them. EXACT whitelist (not the self.assert* prefix —
# `with self.assertEqual(...)` is runtime-broken and must NOT be a with-primitive).
# ---------------------------------------------------------------------------


def test_python_parser_detects_unittest_assert_raises_context():
    text = (
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_raises(self):\n"
        "        with self.assertRaises(DivisionByZeroError):\n"
        "            evaluate('1 / 0')\n"
    )
    blocks = {b.label: b for b in PythonTestBlockProfile().parse_test_blocks(text)}
    assert blocks["test_raises"].has_assertion is True


def test_python_parser_detects_unittest_assertlogs_and_assertwarns_context():
    text = (
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_logs(self):\n"
        "        with self.assertLogs('app'):\n"
        "            run()\n"
        "    def test_warns(self):\n"
        "        with self.assertWarns(DeprecationWarning):\n"
        "            run()\n"
    )
    blocks = {b.label: b for b in PythonTestBlockProfile().parse_test_blocks(text)}
    assert blocks["test_logs"].has_assertion is True
    assert blocks["test_warns"].has_assertion is True


def test_python_parser_still_detects_pytest_raises_and_warns_context():
    text = (
        "import pytest\n"
        "def test_raises():\n"
        "    with pytest.raises(ValueError):\n"
        "        boom()\n"
        "def test_warns():\n"
        "    with pytest.warns(UserWarning):\n"
        "        warn_it()\n"
    )
    blocks = {b.label: b for b in PythonTestBlockProfile().parse_test_blocks(text)}
    assert blocks["test_raises"].has_assertion is True
    assert blocks["test_warns"].has_assertion is True


def test_python_parser_does_not_treat_non_context_assert_as_with_primitive():
    """`with self.assertEqual(...)` is runtime-broken (assertEqual returns no context
    manager) — the With-handler's EXACT whitelist must NOT recognize it (no self.assert*
    prefix at the with position)."""
    text = (
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_bad_context(self):\n"
        "        with self.assertEqual(evaluate('1 + 1'), 2):\n"
        "            pass\n"
    )
    blocks = {b.label: b for b in PythonTestBlockProfile().parse_test_blocks(text)}
    assert blocks["test_bad_context"].has_assertion is False


def test_gate_passes_unittest_assert_raises_with_sut_call(tmp_path):
    """Full gate: a unittest `with self.assertRaises(X): sut()` test is credible (the
    v2.56 regression that 20-false-RED'd the exprcalc-codex run)."""
    project = tmp_path
    _canonical(project, "| VB-DIV0-01 | division by zero |\n")
    _write(project / "src" / "app" / "__init__.py", "")
    _write(
        project / "src" / "app" / "evaluator.py",
        "class DivisionByZeroError(Exception):\n    pass\n\n\ndef evaluate(expr):\n    raise DivisionByZeroError()\n",
    )
    _write(
        project / "tests" / "test_errors.py",
        "import unittest\n"
        "from app.evaluator import DivisionByZeroError, evaluate\n"
        "class EvaluatorErrors(unittest.TestCase):\n"
        "    # codd: covers vb=VB-DIV0-01\n"
        "    def test_direct_zero_denominator_raises(self):\n"
        "        with self.assertRaises(DivisionByZeroError):\n"
        "            evaluate('1 / 0')\n",
    )
    report = build_authenticity_report(
        project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE
    )
    assert report.passed, [v.message for v in report.violations]


# ---------------------------------------------------------------------------
# unittest assert helpers that take the TestCase as a PARAMETER (testcase.assertEqual)
# are credible — but the receiver (testcase) must be EXCLUDED from the argument-anchor
# so a constant-only helper still fails, and a NARROW receiver allowlist keeps a
# namespace helper (helpers.assert_token_values) from being mis-read as a primitive.
# ---------------------------------------------------------------------------


def test_testcase_assert_receivers_unit():
    from codd.vb_marker_authenticity import _testcase_assert_receivers_in as f

    assert f("testcase.assertEqual(a, b)") == {"testcase"}
    assert f("tc.assertRaises(X)") == {"tc"}
    assert f("self.assertTrue(x)") == {"self"}
    # namespace helper / snake assert / non-assert method are NOT receivers
    assert f("helpers.assert_token_values(x)") == set()
    assert f("obj.process(x)") == set()


def test_gate_passes_unittest_helper_asserting_on_its_arg(tmp_path):
    """A shared helper ``def assert_token_values(testcase, tokens, expected):
    testcase.assertEqual(...)`` asserting on its data args is credible (the
    exprcalc-codex remaining-5 false-RED)."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(
        tmp_path / "tests" / "helpers" / "__init__.py",
        "def assert_token_values(testcase, tokens, expected):\n"
        "    actual = list(tokens)\n"
        "    testcase.assertEqual(expected, actual)\n",
    )
    _write(
        tmp_path / "tests" / "test_tok.py",
        "import unittest\n"
        "from tests.helpers import assert_token_values\n"
        "class T(unittest.TestCase):\n"
        "    # codd: covers vb=VB-01\n"
        "    def test_x(self):\n        assert_token_values(self, [1, 2], [1, 2])\n",
    )
    report = build_authenticity_report(tmp_path, config=_scan_cfg(), profile=PY_PROFILE)
    assert report.passed, [(v.kind, v.message) for v in report.violations]


def test_gate_rejects_unittest_helper_constant_only(tmp_path):
    """The receiver must NOT count as an anchor: a helper that only does
    ``testcase.assertEqual(1, 1)`` (no data arg) is a constant no-op → REJECT."""
    _canonical(tmp_path, "| VB-01 | demo |\n")
    _write(
        tmp_path / "tests" / "helpers" / "__init__.py",
        "def check(testcase):\n    testcase.assertEqual(1, 1)\n",
    )
    _write(
        tmp_path / "tests" / "test_x.py",
        "import unittest\n"
        "from tests.helpers import check\n"
        "class T(unittest.TestCase):\n"
        "    # codd: covers vb=VB-01\n"
        "    def test_x(self):\n        check(self)\n",
    )
    report = build_authenticity_report(tmp_path, config=_scan_cfg(), profile=PY_PROFILE)
    assert not report.passed
    assert any(v.kind == "no_assertion" and v.vb_id == "VB-01" for v in report.violations)


def test_python_primitive_regex_narrow_receiver():
    """The primitive regex matches ``testcase.assertEqual(`` (TestCase receiver, CamelCase)
    but NOT a namespace/snake helper (``helpers.assert_token_values(``) nor ``obj.process(``."""
    from codd.vb_marker_authenticity import _PY_PRIMITIVE_ASSERT_RE as R

    assert R.search("    testcase.assertEqual(a, b)")
    assert R.search("    tc.assertRaises(X)")
    assert not R.search("    helpers.assert_token_values(x)")  # namespace + snake -> not primitive
    assert not R.search("    obj.process(x)")
