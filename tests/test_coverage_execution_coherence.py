"""Tests for coverage-execution coherence — the anti-false-green axis binding
STATIC VB coverage to ACTUAL test EXECUTION (design /tmp/gpt_vscope_result.txt).

The load-bearing scenario these encode is the greenfield codex14 false-green:
verify ran the SUT's ``test:unit`` (39 unit tests), exited 0, and declared
"verification passed" while 28 declared behaviors were covered ONLY by e2e files
``test:unit`` never ran. The fix is a PROFILE-OWNED verify campaign that runs the
WHOLE VB surface + a coherence gate that fails on "covered but unexecuted".

The vitest report fixtures here are synthetic but byte-shape-identical to a real
``vitest run --reporter=json`` output (``testResults[].name`` absolute file +
``assertionResults[].status``), so the adapter is exercised against the real
schema without requiring an npm/node toolchain in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd.coverage_execution_coherence import (
    CampaignError,
    CoherenceError,
    RunnerExecution,
    RunnerReportUnsupported,
    VitestJsonReportAdapter,
    build_coherence_report,
    build_test_inventory,
    coherence_gate_applies,
    enforce_coverage_execution_coherence,
    resolve_runner_report_adapter,
    run_verify_campaign,
    supported_runner_report_formats,
)
from codd.project_types import (
    LayoutProfile,
    VerifyCampaignSpec,
    resolve_layout_profile,
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures: a synthetic TS project with unit + e2e VB coverage
# ───────────────────────────────────────────────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _emit_report_command(project: Path, payload: dict, report_relpath: str) -> str:
    """A brace-free shell command that materializes ``payload`` at ``report_relpath``.

    The campaign ``command_template`` is run through ``str.format`` (for the
    ``{test_root}`` / ``{report}`` placeholders), so it must contain NO literal
    ``{``/``}``. We therefore pre-write the JSON to a sidecar file and return a
    plain ``mkdir + cp`` command — exercising the real subprocess + report-read
    path without a node/npm toolchain.
    """
    sidecar = project / "_fixture_report.json"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    dest_dir = str(Path(report_relpath).parent)
    return f"mkdir -p {dest_dir} && cp _fixture_report.json {report_relpath}"


def _ts_project(tmp_path: Path) -> Path:
    """A TS project: 4 VBs, 2 unit-covered, 2 e2e-only, all with real assertions."""
    project = tmp_path
    _write(
        project / "docs" / "test" / "test_strategy.md",
        "| VB | Description |\n"
        "| --- | --- |\n"
        "| VB-UNIT-01 | celsius to fahrenheit conversion |\n"
        "| VB-UNIT-02 | input validation rejects NaN |\n"
        "| VB-E2E-01 | CLI prints converted value |\n"
        "| VB-E2E-02 | CLI exits nonzero on bad arg |\n",
    )
    # Unit tests (real assertions; vitest TS profile recognizes expect()).
    _write(
        project / "tests" / "unit" / "conversion.test.ts",
        'import { describe, it, expect } from "vitest";\n'
        'import { toF } from "../../src/conversion";\n'
        'describe("conv", () => {\n'
        "  // codd: covers vb=VB-UNIT-01\n"
        '  it("c to f", () => { expect(toF(0)).toBe(32); });\n'
        "  // codd: covers vb=VB-UNIT-02\n"
        '  it("rejects nan", () => { expect(() => toF(NaN)).toThrow(); });\n'
        "});\n",
    )
    # E2E tests (real assertions) — ONLY place VB-E2E-* are covered.
    _write(
        project / "tests" / "e2e" / "cli.e2e.test.ts",
        'import { describe, it, expect } from "vitest";\n'
        'import { runCli } from "./helpers/cli";\n'
        'describe("cli e2e", () => {\n'
        "  // codd: covers vb=VB-E2E-01\n"
        '  it("prints value", () => { const r = runCli(["0C"]); expect(r.stdout).toContain("32"); });\n'
        "  // codd: covers vb=VB-E2E-02\n"
        '  it("bad arg", () => { const r = runCli(["xx"]); expect(r.exitCode).not.toBe(0); });\n'
        "});\n",
    )
    _write(
        project / "tests" / "e2e" / "helpers" / "cli.ts",
        "export function runCli(args: string[]) { return { stdout: '32', exitCode: 0 }; }\n",
    )
    _write(project / "src" / "conversion.ts", "export function toF(c: number) { return c; }\n")
    # A package.json so the TS profile / node detection is unambiguous.
    _write(project / "package.json", json.dumps({"name": "tc", "scripts": {"test:unit": "vitest run tests/unit"}}))
    return project


def _ts_profile(project: Path) -> LayoutProfile:
    profile = resolve_layout_profile(
        language="typescript", project_name="tc", project_root=project
    )
    assert profile is not None
    return profile


def _vitest_report(project: Path, *, files: dict[str, list[tuple[str, str]]]) -> RunnerExecution:
    """Build a real-shape vitest JSON report and parse it through the adapter.

    ``files`` maps a project-relative test file to a list of (test_name, status).
    """
    test_results = []
    num_passed = 0
    num_total = 0
    for rel, cases in files.items():
        assertion_results = []
        file_status = "passed"
        for name, status in cases:
            assertion_results.append(
                {"title": name, "fullName": name, "status": status, "ancestorTitles": [], "failureMessages": []}
            )
            num_total += 1
            if status == "passed":
                num_passed += 1
            else:
                file_status = "failed"
        test_results.append(
            {
                "name": str((project / rel).resolve()),
                "status": file_status,
                "assertionResults": assertion_results,
            }
        )
    payload = {
        "numTotalTests": num_total,
        "numPassedTests": num_passed,
        "success": num_passed == num_total,
        "testResults": test_results,
    }
    report_path = project / ".codd" / "verify" / "report.json"
    _write(report_path, json.dumps(payload))
    return VitestJsonReportAdapter().parse(report_path, project_root=project)


# ───────────────────────────────────────────────────────────────────────────
# 4. Runner JSON parse (executed+passed extraction)
# ───────────────────────────────────────────────────────────────────────────


def test_vitest_adapter_extracts_executed_passed_files_and_cases(tmp_path):
    project = _ts_project(tmp_path)
    execution = _vitest_report(
        project,
        files={
            "tests/unit/conversion.test.ts": [("c to f", "passed"), ("rejects nan", "passed")],
            "tests/e2e/cli.e2e.test.ts": [("prints value", "passed"), ("bad arg", "passed")],
        },
    )
    assert execution.executed_passed_files == frozenset(
        {"tests/unit/conversion.test.ts", "tests/e2e/cli.e2e.test.ts"}
    )
    assert execution.executed_failed_files == frozenset()
    assert execution.test_level_available is True
    assert execution.total_cases == 4 and execution.passed_cases == 4
    assert "tests/e2e/cli.e2e.test.ts::prints value" in execution.executed_passed_cases


def test_vitest_adapter_marks_file_failed_when_any_case_fails(tmp_path):
    project = _ts_project(tmp_path)
    execution = _vitest_report(
        project,
        files={
            "tests/unit/conversion.test.ts": [("c to f", "passed"), ("rejects nan", "failed")],
        },
    )
    # A file with ANY non-pass case is NOT a clean execution proof.
    assert "tests/unit/conversion.test.ts" not in execution.executed_passed_files
    assert "tests/unit/conversion.test.ts" in execution.executed_failed_files


def test_vitest_adapter_skipped_case_does_not_make_file_pass(tmp_path):
    project = _ts_project(tmp_path)
    execution = _vitest_report(
        project,
        files={
            "tests/unit/conversion.test.ts": [("c to f", "passed"), ("rejects nan", "skipped")],
        },
    )
    # A skipped case proves nothing — the file is not a clean pass.
    assert "tests/unit/conversion.test.ts" not in execution.executed_passed_files


def test_vitest_adapter_unreadable_report_raises(tmp_path):
    bad = tmp_path / "missing.json"
    with pytest.raises(RunnerReportUnsupported):
        VitestJsonReportAdapter().parse(bad, project_root=tmp_path)
    garbled = tmp_path / "garbled.json"
    garbled.write_text("not json {", encoding="utf-8")
    with pytest.raises(RunnerReportUnsupported):
        VitestJsonReportAdapter().parse(garbled, project_root=tmp_path)


# ───────────────────────────────────────────────────────────────────────────
# 5. TestInventory single-source + kind classification
# ───────────────────────────────────────────────────────────────────────────


def test_test_inventory_classifies_kinds_and_annotates_execution(tmp_path):
    project = _ts_project(tmp_path)
    execution = _vitest_report(
        project,
        files={
            "tests/unit/conversion.test.ts": [("c to f", "passed"), ("rejects nan", "passed")],
        },
    )
    inv = build_test_inventory(project, execution=execution)
    # Both unit and e2e files are in ONE inventory (shared glob), with kinds.
    assert inv.get("tests/unit/conversion.test.ts").kind == "unit"
    assert inv.get("tests/e2e/cli.e2e.test.ts").kind == "e2e"
    # Execution annotation: unit ran+passed; e2e was not in the report.
    assert inv.passed("tests/unit/conversion.test.ts") is True
    assert inv.get("tests/unit/conversion.test.ts").runner_inclusion is True
    assert inv.get("tests/e2e/cli.e2e.test.ts").runner_inclusion is False
    assert inv.get("tests/e2e/cli.e2e.test.ts").execution_status == "not_executed"
    assert inv.e2e_files == ["tests/e2e/cli.e2e.test.ts"]
    assert inv.executed_e2e_files == []


def test_test_inventory_shares_glob_with_vb_audit(tmp_path):
    """The inventory's test-file set is the SAME as the VB audit's matched files
    (no per-gate glob): every file a VB marker matches is in the inventory."""
    project = _ts_project(tmp_path)
    from codd.verifiable_behavior_audit import build_vb_coverage_audit

    audit = build_vb_coverage_audit(project)
    matched = {p for row in audit.rows for p in row.matched_tests}
    inv = build_test_inventory(project)
    for path in matched:
        assert inv.get(path) is not None, f"{path} matched a VB but is absent from the inventory"


# ───────────────────────────────────────────────────────────────────────────
# 1. codex14 scenario: campaign runs unit+e2e → coherence PASS
# ───────────────────────────────────────────────────────────────────────────


def test_coherence_passes_when_campaign_runs_unit_and_e2e(tmp_path):
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    # Full campaign: unit AND e2e executed + passed (what the profile campaign does).
    execution = _vitest_report(
        project,
        files={
            "tests/unit/conversion.test.ts": [("c to f", "passed"), ("rejects nan", "passed")],
            "tests/e2e/cli.e2e.test.ts": [("prints value", "passed"), ("bad arg", "passed")],
        },
    )
    report = build_coherence_report(project, profile=profile, execution=execution)
    assert report.applicable is True
    assert report.passed is True, [v.message for v in report.unverified_vbs]
    assert report.verified_count == 4 and report.unblocked_count == 4
    assert report.executed_e2e_files == 1 and report.e2e_files == 1


# ───────────────────────────────────────────────────────────────────────────
# 2. e2e NOT run → coherence HARD FAIL (unexecuted-covered detection)
# ───────────────────────────────────────────────────────────────────────────


def test_coherence_hard_fails_when_e2e_not_executed(tmp_path):
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    # The FALSE-GREEN command: only unit executed (e2e suite never ran).
    execution = _vitest_report(
        project,
        files={
            "tests/unit/conversion.test.ts": [("c to f", "passed"), ("rejects nan", "passed")],
        },
    )
    report = build_coherence_report(project, profile=profile, execution=execution)
    assert report.passed is False
    # The 2 e2e-only VBs are execution-unverified; the 2 unit VBs are verified.
    unverified_ids = {v.vb_id for v in report.unverified_vbs}
    assert "VB-E2E-01" in unverified_ids and "VB-E2E-02" in unverified_ids
    assert "VB-UNIT-01" not in unverified_ids and "VB-UNIT-02" not in unverified_ids
    assert report.verified_count == 2
    # Every e2e-only failure is the "covering test not executed" reason.
    for vb in report.unverified_vbs:
        if vb.vb_id.startswith("VB-E2E"):
            assert vb.reason == "no_covering_test_executed"


def test_coherence_hard_fails_when_covering_test_ran_but_failed(tmp_path):
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    execution = _vitest_report(
        project,
        files={
            "tests/unit/conversion.test.ts": [("c to f", "passed"), ("rejects nan", "passed")],
            "tests/e2e/cli.e2e.test.ts": [("prints value", "failed"), ("bad arg", "passed")],
        },
    )
    report = build_coherence_report(project, profile=profile, execution=execution)
    assert report.passed is False
    # The e2e file ran but failed → its VBs are unverified with the FAILED reason.
    e2e_vbs = [v for v in report.unverified_vbs if v.vb_id.startswith("VB-E2E")]
    assert e2e_vbs and all(v.reason == "covering_test_failed" for v in e2e_vbs)


def test_coherence_hard_fails_with_no_execution_at_all(tmp_path):
    """No campaign report (execution=None) → every covered VB is unverified.
    This is the strongest form of the unexecuted-covered failure."""
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    report = build_coherence_report(project, profile=profile, execution=None)
    assert report.passed is False
    assert report.verified_count == 0
    assert {v.vb_id for v in report.unverified_vbs} >= {
        "VB-UNIT-01", "VB-UNIT-02", "VB-E2E-01", "VB-E2E-02"
    }


# ───────────────────────────────────────────────────────────────────────────
# 3. e2e exists but scan 0 → observability FAIL
# ───────────────────────────────────────────────────────────────────────────


def test_e2e_observability_failure_when_e2e_surface_unscanned(tmp_path):
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    # Execution present but ran ZERO e2e files (only unit) — the e2e surface
    # exists (e2e files in the inventory + VB markers in e2e files) yet 0 scanned.
    execution = _vitest_report(
        project,
        files={
            "tests/unit/conversion.test.ts": [("c to f", "passed"), ("rejects nan", "passed")],
        },
    )
    report = build_coherence_report(project, profile=profile, execution=execution)
    assert any(e.kind == "e2e_scan_zero" for e in report.observability_errors)


def test_no_observability_error_when_no_e2e_surface(tmp_path):
    """A project with NO e2e files / markers / e2e modality must NOT raise a
    spurious e2e observability error (anti-false-RED)."""
    project = tmp_path
    _write(
        project / "docs" / "test" / "test_strategy.md",
        "| VB | Description |\n| --- | --- |\n| VB-UNIT-01 | adds |\n",
    )
    _write(
        project / "tests" / "unit" / "add.test.ts",
        'import { it, expect } from "vitest";\n'
        "// codd: covers vb=VB-UNIT-01\n"
        'it("adds", () => { expect(1 + 1).toBe(2); });\n',
    )
    _write(project / "package.json", json.dumps({"name": "x"}))
    profile = _ts_profile(project)
    execution = _vitest_report(
        project, files={"tests/unit/add.test.ts": [("adds", "passed")]}
    )
    report = build_coherence_report(project, profile=profile, execution=execution)
    assert report.observability_errors == []
    assert report.passed is True


def test_no_e2e_files_executed_but_no_surface_passes(tmp_path):
    """The unit-only project's full-campaign run passes (no e2e to miss)."""
    test_no_observability_error_when_no_e2e_surface(tmp_path)


# ───────────────────────────────────────────────────────────────────────────
# 6. Profile campaign resolution + adapter registration
# ───────────────────────────────────────────────────────────────────────────


def test_typescript_profile_declares_vitest_campaign(tmp_path):
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    campaign = profile.verify_campaign
    assert campaign is not None
    assert campaign.report_format == "vitest-json"
    # Runs the WHOLE test root with the JSON reporter (not a single SUT script).
    cmd = campaign.resolve_command(test_root=profile.test_root, report_path=campaign.report_relpath)
    assert "vitest run" in cmd and profile.test_root in cmd and "--reporter=json" in cmd
    assert "test:unit" not in cmd  # never a single SUT script
    # The adapter resolves from the profile.
    assert profile.runner_report_adapter() is not None
    assert coherence_gate_applies(profile) is True


def test_python_profile_has_no_campaign_gate_is_noop(tmp_path):
    profile = resolve_layout_profile(
        language="python", project_name="todo", project_root=tmp_path
    )
    assert profile is not None
    assert profile.verify_campaign is None
    assert profile.runner_report_adapter() is None
    assert coherence_gate_applies(profile) is False
    # The enforce entry point is a clean NO-OP for a no-campaign stack.
    report = enforce_coverage_execution_coherence(tmp_path, profile, echo=lambda _m: None)
    assert report.applicable is False


def test_vitest_json_adapter_is_registered():
    assert "vitest-json" in supported_runner_report_formats()
    assert resolve_runner_report_adapter("vitest-json") is not None
    # Documented-but-unimplemented formats degrade EXPLICITLY (None, not silent).
    assert resolve_runner_report_adapter("pytest-junit-xml") is None
    assert resolve_runner_report_adapter("go-test-json") is None
    assert resolve_runner_report_adapter(None) is None


def test_campaign_with_unknown_report_format_degrades_explicitly(tmp_path):
    """A profile that declares a campaign whose report_format has no adapter is
    NOT applicable (the gate cannot read it) — surfaced, never a silent green."""
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    broken = LayoutProfile(
        language=profile.language,
        package_name=profile.package_name,
        source_root=profile.source_root,
        package_root=profile.package_root,
        test_root=profile.test_root,
        verify_campaign=VerifyCampaignSpec(
            command_template="echo nothing",
            report_relpath=".codd/verify/x.json",
            report_format="totally-unknown-format",
        ),
    )
    assert coherence_gate_applies(broken) is False
    # run_verify_campaign refuses (no adapter) rather than silently passing.
    with pytest.raises(CampaignError):
        run_verify_campaign(project, broken, echo=lambda _m: None)


# ───────────────────────────────────────────────────────────────────────────
# Campaign execution (subprocess) + the enforce entry point
# ───────────────────────────────────────────────────────────────────────────


def test_run_verify_campaign_parses_a_real_report(tmp_path):
    """A campaign whose command WRITES a vitest-shaped report is parsed (no npm:
    the command is a python one-liner that emits the report file)."""
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    # Replace the command with one that emits a full-campaign report deterministically.
    payload = {
        "numTotalTests": 4,
        "numPassedTests": 4,
        "success": True,
        "testResults": [
            {
                "name": str((project / "tests/unit/conversion.test.ts").resolve()),
                "status": "passed",
                "assertionResults": [
                    {"title": "c to f", "fullName": "c to f", "status": "passed"},
                    {"title": "rejects nan", "fullName": "rejects nan", "status": "passed"},
                ],
            },
            {
                "name": str((project / "tests/e2e/cli.e2e.test.ts").resolve()),
                "status": "passed",
                "assertionResults": [
                    {"title": "prints value", "fullName": "prints value", "status": "passed"},
                    {"title": "bad arg", "fullName": "bad arg", "status": "passed"},
                ],
            },
        ],
    }
    report_file = profile.verify_campaign.report_relpath
    campaign = VerifyCampaignSpec(
        command_template=_emit_report_command(project, payload, report_file),
        report_relpath=report_file,
        report_format="vitest-json",
    )
    profile2 = LayoutProfile(
        language=profile.language,
        package_name=profile.package_name,
        source_root=profile.source_root,
        package_root=profile.package_root,
        test_root=profile.test_root,
        verify_campaign=campaign,
    )
    run = run_verify_campaign(project, profile2, echo=lambda _m: None)
    assert run.execution.total_cases == 4
    assert "tests/e2e/cli.e2e.test.ts" in run.execution.executed_passed_files


def test_run_verify_campaign_errors_when_no_report_written(tmp_path):
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    campaign = VerifyCampaignSpec(
        command_template="true",  # writes no report
        report_relpath=".codd/verify/never.json",
        report_format="vitest-json",
    )
    profile2 = LayoutProfile(
        language=profile.language,
        package_name=profile.package_name,
        source_root=profile.source_root,
        package_root=profile.package_root,
        test_root=profile.test_root,
        verify_campaign=campaign,
    )
    with pytest.raises(CampaignError):
        run_verify_campaign(project, profile2, echo=lambda _m: None)


def test_enforce_raises_coherence_error_on_unexecuted_e2e(tmp_path):
    """The greenfield-verify entry point raises CoherenceError when the campaign
    leaves e2e-only VBs unexecuted (end-to-end through the subprocess)."""
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    # A campaign that only emits unit executions (the false-green shape).
    payload = {
        "numTotalTests": 2,
        "numPassedTests": 2,
        "success": True,
        "testResults": [
            {
                "name": str((project / "tests/unit/conversion.test.ts").resolve()),
                "status": "passed",
                "assertionResults": [
                    {"title": "c to f", "fullName": "c to f", "status": "passed"},
                    {"title": "rejects nan", "fullName": "rejects nan", "status": "passed"},
                ],
            }
        ],
    }
    report_file = ".codd/verify/unit-only.json"
    campaign = VerifyCampaignSpec(
        command_template=_emit_report_command(project, payload, report_file),
        report_relpath=report_file,
        report_format="vitest-json",
    )
    profile2 = LayoutProfile(
        language=profile.language,
        package_name=profile.package_name,
        source_root=profile.source_root,
        package_root=profile.package_root,
        test_root=profile.test_root,
        verify_campaign=campaign,
    )
    with pytest.raises(CoherenceError) as exc:
        enforce_coverage_execution_coherence(project, profile2, echo=lambda _m: None)
    assert "VB-E2E-01" in str(exc.value) or "execution" in str(exc.value).lower()


# ───────────────────────────────────────────────────────────────────────────
# Authenticity reconciliation: an inauthentic e2e marker is not execution proof
# ───────────────────────────────────────────────────────────────────────────


def test_inauthentic_skipped_e2e_cover_is_not_execution_verified(tmp_path):
    """A VB whose ONLY covering marker sits on a skipped e2e test is not
    execution-verified even if that file appears in the runner report — the
    coherence gate consults authenticity, not just file presence."""
    project = tmp_path
    _write(
        project / "docs" / "test" / "test_strategy.md",
        "| VB | Description |\n| --- | --- |\n| VB-E2E-09 | skipped behavior |\n",
    )
    _write(
        project / "tests" / "e2e" / "skip.e2e.test.ts",
        'import { describe, it, expect } from "vitest";\n'
        'describe("g", () => {\n'
        "  // codd: covers vb=VB-E2E-09\n"
        '  it.skip("never runs", () => { expect(1).toBe(1); });\n'
        '  it("real", () => { expect(2).toBe(2); });\n'
        "});\n",
    )
    _write(project / "package.json", json.dumps({"name": "s"}))
    profile = _ts_profile(project)
    # The file is reported as executed+passed (the non-skipped case ran), yet the
    # VB's marker is on the SKIPPED test → authenticity drops it → unverified.
    execution = _vitest_report(
        project, files={"tests/e2e/skip.e2e.test.ts": [("real", "passed")]}
    )
    report = build_coherence_report(project, profile=profile, execution=execution)
    ids = {v.vb_id for v in report.unverified_vbs}
    assert "VB-E2E-09" in ids


# ───────────────────────────────────────────────────────────────────────────
# Blocked VBs are exempt (they have an explicit blocker, not an execution claim)
# ───────────────────────────────────────────────────────────────────────────


def test_blocked_vb_is_exempt_from_execution_coherence(tmp_path):
    project = tmp_path
    _write(
        project / "docs" / "test" / "test_strategy.md",
        "| VB | Description |\n| --- | --- |\n"
        "| VB-UNIT-01 | adds |\n| VB-BLK-01 | needs external service |\n",
    )
    _write(
        project / "tests" / "unit" / "add.test.ts",
        'import { it, expect } from "vitest";\n'
        "// codd: covers vb=VB-UNIT-01\n"
        'it("adds", () => { expect(1 + 1).toBe(2); });\n'
        "// codd: blocked vb=VB-BLK-01 reason=external_dependency\n",
    )
    _write(project / "package.json", json.dumps({"name": "b"}))
    profile = _ts_profile(project)
    execution = _vitest_report(
        project, files={"tests/unit/add.test.ts": [("adds", "passed")]}
    )
    report = build_coherence_report(project, profile=profile, execution=execution)
    # VB-BLK-01 is blocked → not in the unblocked set → never an unverified failure.
    assert "VB-BLK-01" not in {v.vb_id for v in report.unverified_vbs}
    assert report.passed is True
