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

from codd.project_types import LayoutProfile
from codd.vb_marker_authenticity import (
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


def test_existing_assert_true_marker_still_passes(tmp_path):
    """Back-compat: the existing gate tests mark `def t(): assert True` — that has
    an assertion, so authenticity must keep PASSING it (no over-tightening)."""
    project = tmp_path
    _canonical(project, "| VB-01 | a |\n| VB-02 | b |\n")
    _write(
        project / "tests" / "test_app.py",
        "# codd: covers vb=VB-01\n"
        "# codd: covers vb=VB-02\n"
        "def test_app():\n    assert True\n",
    )
    report = build_authenticity_report(project, config={"scan": {"test_dirs": ["tests/"]}}, profile=PY_PROFILE)
    assert report.passed
