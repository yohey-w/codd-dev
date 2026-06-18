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
# a false-green when silently degraded — strict mode honest-fails it; an
# UNSUPPORTED file still degrades (never a false-RED).
# ---------------------------------------------------------------------------


def test_strict_observability_flags_recognized_file_with_no_parseable_test(tmp_path):
    """KEYSTONE: a .test.ts file the TS adapter RECOGNIZES but parses ZERO test
    blocks out of, bearing a live marker. Non-strict (default) degrades to a PASS
    (the pre-gate false-green); strict makes it an unobservable_test_structure
    VIOLATION."""
    project = tmp_path
    _canonical(project, "| VB-1 | does a thing |\n")
    # A recognized test file (.test.ts) with a live marker but NO it()/test() block.
    _write(
        project / "tests" / "empty.test.ts",
        "// codd: covers vb=VB-1\nexport const helper = () => 42;\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    # Non-strict (back-compat): recognized-but-unparseable → degraded → PASSES.
    lax = build_authenticity_report(project, config=config, profile=TS_PROFILE)
    assert "tests/empty.test.ts" in lax.degraded_paths
    assert lax.passed is True
    # Strict: an unobservable coverage claim in a SUPPORTED stack → hard violation.
    strict = build_authenticity_report(
        project, config=config, profile=TS_PROFILE, strict_observability=True
    )
    assert strict.passed is False
    assert "unobservable_test_structure" in {v.kind for v in strict.violations}
    assert any(v.vb_id == "VB-1" for v in strict.violations)
    # Not double-counted as degraded once it became a violation.
    assert "tests/empty.test.ts" not in strict.degraded_paths


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
        'it("adds", () => { expect(1 + 1).toBe(2); });\n',
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
