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
import shlex
from pathlib import Path

import pytest

from codd.coverage_execution_coherence import (
    CampaignError,
    CoherenceError,
    GoTestJsonReportAdapter,
    RunnerExecution,
    RunnerReportUnsupported,
    VitestJsonReportAdapter,
    build_coherence_report,
    build_test_inventory,
    coherence_gate_applies,
    enforce_campaign_clean_execution,
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
        # GENUINE observation: references the SUT call ``add`` (a non-ignored
        # name), so the marker-authenticity gate credits it. A constant
        # ``expect(1 + 1).toBe(2)`` is now ``constant_direct`` (proves no behavior);
        # this test exercises execution-coherence/observability, not assertion
        # evidence, so it uses a real covering assertion.
        'it("adds", () => { expect(add(1, 1)).toBe(2); });\n',
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
    # go-test-json is now registered (the Go anti-false-green adapter).
    assert "go-test-json" in supported_runner_report_formats()
    assert resolve_runner_report_adapter("go-test-json") is not None
    # The remaining documented-but-unimplemented format degrades EXPLICITLY (None).
    assert resolve_runner_report_adapter("pytest-junit-xml") is None
    assert resolve_runner_report_adapter(None) is None


def test_compiler_runner_report_adapters_resolve_via_single_source():
    """The compiler-language runner-report adapters (surefire-xml / ctest-junit /
    dotnet-trx) registered in ``codd.languages.builtin_adapters`` resolve through
    the SAME resolver the coverage gate uses.

    Before the registries were unified these adapters lived ONLY on
    ``default_adapter_registry`` while ``resolve_runner_report_adapter`` read a
    second LOCAL table (``vitest-json`` / ``go-test-json`` only), so a profile that
    wired a Java/C#/C++ verify campaign would resolve a ``None`` adapter and
    ``certify_verify_campaign_observable`` would honest-FAIL the build. With the
    single-source registry they resolve to their concrete adapters, and they appear
    in ``supported_runner_report_formats`` consistently with resolution.
    """

    from codd.languages.adapters.runner_report import (
        CTestJunitReportAdapter,
        DotnetTrxReportAdapter,
        SurefireXmlReportAdapter,
    )

    assert isinstance(resolve_runner_report_adapter("surefire-xml"), SurefireXmlReportAdapter)
    assert isinstance(resolve_runner_report_adapter("ctest-junit"), CTestJunitReportAdapter)
    assert isinstance(resolve_runner_report_adapter("dotnet-trx"), DotnetTrxReportAdapter)

    supported = set(supported_runner_report_formats())
    assert {"surefire-xml", "ctest-junit", "dotnet-trx"} <= supported


def test_existing_runner_report_resolution_unchanged_after_unification():
    """REGRESSION LOCK: unifying the source must not change the existing
    ``vitest-json`` / ``go-test-json`` resolution — same adapter types, same
    case-insensitive normalization, same EXPLICIT ``None`` for unknown/empty/None
    (never a silent green for an unreadable campaign report)."""

    from codd.languages.adapters.runner_report import (
        GoTestJsonReportAdapter,
        VitestJsonReportAdapter,
    )

    assert isinstance(resolve_runner_report_adapter("vitest-json"), VitestJsonReportAdapter)
    assert isinstance(resolve_runner_report_adapter("go-test-json"), GoTestJsonReportAdapter)
    # Case-insensitivity + surrounding-whitespace tolerance is preserved.
    assert isinstance(resolve_runner_report_adapter("  VITEST-JSON  "), VitestJsonReportAdapter)
    assert isinstance(resolve_runner_report_adapter("Go-Test-Json"), GoTestJsonReportAdapter)
    # Unknown / not-yet-implemented / empty / None still degrade EXPLICITLY to None.
    assert resolve_runner_report_adapter("pytest-junit-xml") is None
    assert resolve_runner_report_adapter("totally-unknown-format") is None
    assert resolve_runner_report_adapter("") is None
    assert resolve_runner_report_adapter(None) is None
    # vitest-json / go-test-json remain advertised as supported.
    assert {"vitest-json", "go-test-json"} <= set(supported_runner_report_formats())


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


def test_run_verify_campaign_parses_a_directory_shaped_report(tmp_path):
    """Report shape is a filesystem question, not a language one: Maven Surefire
    writes ``target/surefire-reports/`` as a DIRECTORY of one ``TEST-<class>.xml``
    per test class (not a single file like vitest-json/dotnet-trx). Regression
    test for a real bug: run_verify_campaign's stale-cleanup and "produced no
    report" checks originally assumed ``report_path`` was always a file
    (``.unlink()`` / ``.is_file()``), so a directory-shaped report would either
    crash on cleanup or be misreported as "no report produced" even when Surefire
    wrote real, parseable XML.
    """
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    report_dir = ".codd/verify/surefire-reports"
    xml_one = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<testsuite name="com.example.FooTest" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="com.example.FooTest" name="testAdds" time="0.01"/>'
        "</testsuite>"
    )
    xml_two = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<testsuite name="com.example.BarTest" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="com.example.BarTest" name="testSubtracts" time="0.01"/>'
        "</testsuite>"
    )
    (project / report_dir).mkdir(parents=True, exist_ok=True)
    # A stale report from a "prior run" — must be gone after this run's cleanup,
    # not merely shadowed, so a campaign that legitimately drops a class's file
    # (e.g. a class was deleted) can't resurrect its old evidence.
    (project / report_dir / "TEST-com.example.Stale.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<testsuite name="com.example.Stale" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="com.example.Stale" name="testGhost" time="0.01"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    command = (
        f"mkdir -p {report_dir} && "
        f"printf '%s' {shlex.quote(xml_one)} > {report_dir}/TEST-com.example.FooTest.xml && "
        f"printf '%s' {shlex.quote(xml_two)} > {report_dir}/TEST-com.example.BarTest.xml"
    )
    campaign = VerifyCampaignSpec(
        command_template=command,
        report_relpath=report_dir,
        report_format="surefire-xml",
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
    assert run.execution.total_cases == 2
    assert not (project / report_dir / "TEST-com.example.Stale.xml").exists()


def test_run_verify_campaign_errors_when_report_directory_is_empty(tmp_path):
    """An existing-but-empty report directory is exactly as "no report produced"
    as a missing path — never a silent pass (anti-false-green)."""
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    report_dir = ".codd/verify/surefire-reports"
    campaign = VerifyCampaignSpec(
        command_template=f"mkdir -p {report_dir}",  # creates the dir, writes nothing into it
        report_relpath=report_dir,
        report_format="surefire-xml",
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
        # GENUINE observation: references the SUT call ``add`` (a non-ignored
        # name), so the marker-authenticity gate credits it. A constant
        # ``expect(1 + 1).toBe(2)`` is now ``constant_direct`` (proves no behavior);
        # this test exercises execution-coherence/observability, not assertion
        # evidence, so it uses a real covering assertion.
        'it("adds", () => { expect(add(1, 1)).toBe(2); });\n'
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


# ───────────────────────────────────────────────────────────────────────────
# 6. campaign clean-execution gate (contract verify.campaign.clean_execution.v1)
#    A failing test that covers NO declared VB — or a non-zero runner exit — is
#    invisible to build_coherence_report (it reconciles only UNBLOCKED VBs), so
#    it would pass the per-VB coherence gate alone = a false-green. The
#    clean-execution gate makes the campaign result itself a green authority.
# ───────────────────────────────────────────────────────────────────────────


def test_clean_execution_failed_file_raises():
    """A campaign whose report has ANY failed executed test file is hard-RED."""
    execution = RunnerExecution(
        executed_passed_files=frozenset({"tests/unit/ok.test.ts"}),
        executed_failed_files=frozenset({"tests/e2e/non_vb.e2e.test.ts"}),
        test_level_available=True,
        total_cases=2,
        passed_cases=1,
    )
    with pytest.raises(CoherenceError) as exc:
        enforce_campaign_clean_execution(execution, 1)
    assert "non_vb.e2e.test.ts" in str(exc.value)


def test_clean_execution_nonzero_exit_raises():
    """No failed FILES parsed, but the runner itself exited non-zero → still RED
    (the campaign did not cleanly succeed; an unobservable failure is not green)."""
    execution = RunnerExecution(
        executed_passed_files=frozenset({"tests/unit/ok.test.ts"}),
        test_level_available=True,
        total_cases=1,
        passed_cases=1,
    )
    with pytest.raises(CoherenceError) as exc:
        enforce_campaign_clean_execution(execution, 2)
    assert "non-zero" in str(exc.value) and "2" in str(exc.value)


def test_clean_execution_all_pass_exit_zero_is_ok():
    """A clean campaign (no failed files, exit 0) passes the gate — no false-RED."""
    execution = RunnerExecution(
        executed_passed_files=frozenset(
            {"tests/unit/ok.test.ts", "tests/e2e/ok.e2e.test.ts"}
        ),
        test_level_available=True,
        total_cases=2,
        passed_cases=2,
    )
    # No raise.
    enforce_campaign_clean_execution(execution, 0)


def test_clean_execution_closes_false_green_for_failing_non_vb_test(tmp_path):
    """KEYSTONE (registry negative fixture): every VB covering file PASSED — so
    build_coherence_report alone is GREEN — but a test covering NO declared VB
    FAILED. The per-VB coherence gate misses it (it reconciles only VBs); the
    clean-execution gate makes the run RED. This is the exact false-green the
    contract closes."""
    project = _ts_project(tmp_path)
    profile = _ts_profile(project)
    # A non-VB test file (covers no declared VB) that FAILS, alongside all 4 VB
    # covering files passing.
    _write(
        project / "tests" / "integration" / "smoke.test.ts",
        'import { describe, it, expect } from "vitest";\n'
        'describe("smoke", () => { it("db", () => { expect(1).toBe(2); }); });\n',
    )
    execution = _vitest_report(
        project,
        files={
            "tests/unit/conversion.test.ts": [("c to f", "passed"), ("rejects nan", "passed")],
            "tests/e2e/cli.e2e.test.ts": [("prints value", "passed"), ("bad arg", "passed")],
            "tests/integration/smoke.test.ts": [("db", "failed")],
        },
    )
    # The per-VB coherence gate ALONE would GREEN this run: all 4 VBs verified.
    report = build_coherence_report(project, profile=profile, execution=execution)
    assert report.passed is True, [v.message for v in report.unverified_vbs]
    assert "tests/integration/smoke.test.ts" in execution.executed_failed_files
    # The clean-execution gate closes the hole → hard RED.
    with pytest.raises(CoherenceError) as exc:
        enforce_campaign_clean_execution(execution, 1)
    assert "smoke.test.ts" in str(exc.value)


def test_clean_execution_covers_environment_skipped_tests(tmp_path):
    """environment.skipped_tests_not_green: a SKIPPED test (e.g. its environment is
    missing) makes its file UNCLEAN → executed_failed_files → the clean-execution
    gate reds the run EVEN at exit_code 0 (vitest skips do not fail the exit code).
    A skipped test proves nothing; the run is not green. This contract is covered by
    the v2.39 clean-execution gate — the adapter already routes any skipped case to
    executed_failed_files (see test_vitest_adapter_skipped_case_does_not_make_file_pass)."""
    project = _ts_project(tmp_path)
    execution = _vitest_report(
        project,
        files={"tests/unit/conversion.test.ts": [("c to f", "passed"), ("rejects nan", "skipped")]},
    )
    # The skipped-bearing file is unclean → executed_failed_files (the adapter rule).
    assert "tests/unit/conversion.test.ts" in execution.executed_failed_files
    # → RED even though the runner exited 0 (skips do not fail vitest's exit code).
    with pytest.raises(CoherenceError):
        enforce_campaign_clean_execution(execution, 0)


# ───────────────────────────────────────────────────────────────────────────
# go-test-json adapter (the Go anti-false-green RunnerReportAdapter)
#
# The fixtures below are synthetic but BYTE-SHAPE-IDENTICAL to a real
# ``go test -json ./...`` stream (line-delimited JSON, one ``{"Action":...,
# "Package":...,"Test":...}`` per line; subtests as ``TestX/sub``; a package-level
# terminal event with no ``Test``; ``build-fail`` + package ``fail`` with no
# ``Test`` for a non-compiling package), captured from a real ``go`` run — so the
# adapter is exercised against the real schema without a ``go`` toolchain in CI.
# ───────────────────────────────────────────────────────────────────────────


def _go_jsonl(events: list[dict]) -> str:
    """Render runner events as a go-test-json (line-delimited JSON) stream."""
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _go_event(action: str, package: str, test: str | None = None, **extra) -> dict:
    e: dict = {"Time": "2026-06-20T00:00:00Z", "Action": action, "Package": package}
    if test is not None:
        e["Test"] = test
    e.update(extra)
    return e


def _go_project(tmp_path: Path) -> Path:
    """A Go module: go.mod + a package with two _test.go files (one mixed pass+skip)."""
    project = tmp_path
    _write(project / "go.mod", "module example.com/m\n\ngo 1.21\n")
    _write(
        project / "docs" / "test" / "test_strategy.md",
        "| VB | Description |\n| --- | --- |\n"
        "| VB-CREATE-01 | create increments |\n"
        "| VB-SKIP-01 | skipped behavior |\n"
        "| VB-GET-01 | get echoes |\n",
    )
    _write(
        project / "internal" / "store" / "store.go",
        "package store\n\nfunc Create(x int) int { return x + 1 }\nfunc Get(x int) int { return x }\n",
    )
    # create_test.go: a real (passing) TestCreate with a subtest + a SKIPPED test.
    _write(
        project / "internal" / "store" / "create_test.go",
        "package store\n\nimport \"testing\"\n\n"
        "// codd: covers vb=VB-CREATE-01\n"
        "func TestCreate(t *testing.T) {\n"
        "\tif Create(1) != 2 { t.Fatalf(\"want 2 got %d\", Create(1)) }\n"
        "\tt.Run(\"sub\", func(t *testing.T) { if Create(2) != 3 { t.Fatalf(\"want 3\") } })\n"
        "}\n\n"
        "// codd: covers vb=VB-SKIP-01\n"
        "func TestSkipped(t *testing.T) { t.Skip(\"not yet\") }\n",
    )
    # get_test.go: a real (passing) TestGet.
    _write(
        project / "internal" / "store" / "get_test.go",
        "package store\n\nimport \"testing\"\n\n"
        "// codd: covers vb=VB-GET-01\n"
        "func TestGet(t *testing.T) { if Get(5) != 5 { t.Fatalf(\"want 5 got %d\", Get(5)) } }\n",
    )
    return project


def _go_profile(project: Path) -> LayoutProfile:
    """A Go LayoutProfile declaring the go-test-json verify campaign.

    ``resolve_layout_profile`` does not yet synthesize a Go profile (go.yaml is the
    declarative source; greenfield Go wiring is staged separately), so the profile
    is constructed directly — exactly as the vitest tests construct broken/derived
    profiles. ``language="go"`` makes ``test_block_profile()`` resolve
    ``GoTestBlockProfile`` and ``runner_report_adapter()`` resolve the go-test-json
    adapter from the campaign's ``report_format``.
    """
    return LayoutProfile(
        language="go",
        package_name="m",
        source_root=".",
        package_root=".",
        test_root=".",
        verify_campaign=VerifyCampaignSpec(
            command_template="go test -json ./... > {report}",
            report_relpath=".codd/verify/go-test.jsonl",
            report_format="go-test-json",
        ),
    )


def test_go_adapter_parses_passed_tests_as_executed_passed(tmp_path):
    """A go-test-json stream where TestCreate (+ its subtest) and TestGet pass:
    their FILES register as executed+passed with per-test cases."""
    project = _go_project(tmp_path)
    pkg = "example.com/m/internal/store"
    stream = _go_jsonl(
        [
            _go_event("run", pkg, "TestCreate"),
            _go_event("run", pkg, "TestCreate/sub"),
            _go_event("pass", pkg, "TestCreate/sub", Elapsed=0),
            _go_event("pass", pkg, "TestCreate", Elapsed=0),
            _go_event("run", pkg, "TestGet"),
            _go_event("pass", pkg, "TestGet", Elapsed=0),
            _go_event("pass", pkg, Elapsed=0.01),  # package-level OK (no Test)
        ]
    )
    report = project / ".codd" / "verify" / "go-test.jsonl"
    _write(report, stream)
    execution = GoTestJsonReportAdapter().parse(report, project_root=project)
    # This stream omits TestSkipped entirely, so BOTH files ran clean and passed.
    assert "internal/store/get_test.go" in execution.executed_passed_files
    assert "internal/store/create_test.go" in execution.executed_passed_files
    assert execution.executed_failed_files == frozenset()
    # subtests fold into the parent func's file; the parent passed-case is recorded.
    assert "internal/store/create_test.go::TestCreate" in execution.executed_passed_cases
    assert "internal/store/get_test.go::TestGet" in execution.executed_passed_cases
    assert execution.test_level_available is True
    # 3 terminal pass cases (TestCreate, TestCreate/sub, TestGet).
    assert execution.total_cases == 3 and execution.passed_cases == 3


def test_go_adapter_skipped_vb_test_is_not_green(tmp_path):
    """ANTI-FALSE-GREEN: a VB-marked Go test that is SKIPPED must NOT count green.
    TestSkipped (covers VB-SKIP-01) is reported ``skip`` → its file is NOT in
    executed_passed_files, and the coherence gate flags VB-SKIP-01 as unverified."""
    project = _go_project(tmp_path)
    profile = _go_profile(project)
    pkg = "example.com/m/internal/store"
    stream = _go_jsonl(
        [
            _go_event("pass", pkg, "TestCreate/sub", Elapsed=0),
            _go_event("pass", pkg, "TestCreate", Elapsed=0),
            _go_event("skip", pkg, "TestSkipped", Elapsed=0),  # the VB-marked SKIP
            _go_event("pass", pkg, "TestGet", Elapsed=0),
            _go_event("pass", pkg, Elapsed=0.01),
        ]
    )
    report = project / ".codd" / "verify" / "go-test.jsonl"
    _write(report, stream)
    execution = GoTestJsonReportAdapter().parse(report, project_root=project)
    # PER-CASE authority: TestCreate PASSED → its case key is present EVEN THOUGH its
    # file is coarse-tainted by TestSkipped's skip (the file-level signal still feeds
    # clean-execution). TestSkipped is a SKIP → its case key is absent (not a pass).
    assert "internal/store/create_test.go::TestCreate" in execution.executed_passed_cases
    assert "internal/store/create_test.go::TestSkipped" not in execution.executed_passed_cases
    # Coarse file signal: the skip taints the file (for clean-execution), but this does
    # NOT gate the passed VB below (Option A — per-case reconciliation).
    assert "internal/store/create_test.go" in execution.executed_failed_files
    report_obj = build_coherence_report(project, profile=profile, execution=execution)
    unverified = {v.vb_id for v in report_obj.unverified_vbs}
    # KEYSTONE (anti-false-RED): VB-CREATE-01 (covered by the PASSED TestCreate) is
    # VERIFIED despite the sibling TestSkipped skip in the SAME file — file-level taint
    # would have wrongly failed it (the exact false-RED Option A avoids for Go).
    assert "VB-CREATE-01" not in unverified
    # ANTI-FALSE-GREEN: the SKIPPED VB is NOT green (its case key never passed).
    assert "VB-SKIP-01" in unverified
    # And the run is still RED overall — a skip anywhere reds the campaign (the coarse
    # file signal drives the clean-execution gate).
    with pytest.raises(CoherenceError):
        enforce_campaign_clean_execution(execution, 0)


def test_go_adapter_missing_vb_test_is_flagged(tmp_path):
    """ANTI-FALSE-GREEN: a VB-marked Go test DECLARED statically but ABSENT from the
    report (never executed — e.g. filtered by a build tag / -run) is NOT green. The
    runner stream omits TestGet entirely → its file stays not_executed → VB-GET-01
    is execution-unverified."""
    project = _go_project(tmp_path)
    profile = _go_profile(project)
    pkg = "example.com/m/internal/store"
    # TestGet is NEVER emitted (declared in get_test.go but absent from the run).
    stream = _go_jsonl(
        [
            _go_event("pass", pkg, "TestCreate/sub", Elapsed=0),
            _go_event("pass", pkg, "TestCreate", Elapsed=0),
            _go_event("pass", pkg, "TestSkipped", Elapsed=0),  # make create file clean here
            _go_event("pass", pkg, Elapsed=0.01),
        ]
    )
    report = project / ".codd" / "verify" / "go-test.jsonl"
    _write(report, stream)
    execution = GoTestJsonReportAdapter().parse(report, project_root=project)
    # get_test.go is in NEITHER passed nor failed (it never ran) → not_executed.
    assert "internal/store/get_test.go" not in execution.executed_files
    inv = build_test_inventory(project, execution=execution)
    assert inv.get("internal/store/get_test.go").execution_status == "not_executed"
    report_obj = build_coherence_report(project, profile=profile, execution=execution)
    vb = {v.vb_id: v for v in report_obj.unverified_vbs}
    assert "VB-GET-01" in vb
    assert vb["VB-GET-01"].reason == "no_covering_test_executed"


def test_go_adapter_package_build_failure_is_honest_fail(tmp_path):
    """ANTI-FALSE-GREEN: a package that FAILED TO BUILD (a ``build-fail`` + a
    package-level ``fail`` with no ``Test``) must NOT be a silent pass — every
    _test.go in that package's directory is marked failed (nothing in it ran)."""
    project = _go_project(tmp_path)
    pkg = "example.com/m/internal/store"
    stream = _go_jsonl(
        [
            # Go emits build-output (often non-Test) then a package-level fail w/ no Test.
            {"ImportPath": pkg + " [" + pkg + ".test]", "Action": "build-output",
             "Output": "internal/store/store.go:3:1: undefined: nope\n"},
            {"ImportPath": pkg + " [" + pkg + ".test]", "Action": "build-fail"},
            _go_event("output", pkg, Output="FAIL\t" + pkg + " [build failed]\n"),
            _go_event("fail", pkg, Elapsed=0, FailedBuild=pkg + " [" + pkg + ".test]"),
        ]
    )
    report = project / ".codd" / "verify" / "go-test.jsonl"
    _write(report, stream)
    execution = GoTestJsonReportAdapter().parse(report, project_root=project)
    # Both _test.go in internal/store are failed (the package never compiled).
    assert execution.executed_passed_files == frozenset()
    assert "internal/store/create_test.go" in execution.executed_failed_files
    assert "internal/store/get_test.go" in execution.executed_failed_files
    # → clean-execution gate reds the run.
    with pytest.raises(CoherenceError):
        enforce_campaign_clean_execution(execution, 1)

    # Also honest-fail when ONLY the structured ``build-fail`` event is present (no
    # package-level ``fail`` summary) — the build-fail carries ImportPath, not Test,
    # and must still taint every _test.go in the dir.
    only_build_fail = _go_jsonl(
        [
            {"ImportPath": pkg + " [" + pkg + ".test]", "Action": "build-output",
             "Output": "internal/store/store.go:3:1: undefined: nope\n"},
            {"ImportPath": pkg + " [" + pkg + ".test]", "Action": "build-fail"},
        ]
    )
    report2 = project / ".codd" / "verify" / "go-test2.jsonl"
    _write(report2, only_build_fail)
    execution2 = GoTestJsonReportAdapter().parse(report2, project_root=project)
    assert "internal/store/create_test.go" in execution2.executed_failed_files
    assert "internal/store/get_test.go" in execution2.executed_failed_files


def test_go_adapter_subtests_parse_and_fold_to_parent_file(tmp_path):
    """A subtest (``TestX/sub``) is attributed to the SAME file as its parent
    ``TestX`` (the func-name before the first ``/`` is the join key)."""
    project = _go_project(tmp_path)
    pkg = "example.com/m/internal/store"
    # Only TestCreate (+ subtest) + TestGet; no skip → both files clean.
    _write(
        project / "internal" / "store" / "create_test.go",
        "package store\n\nimport \"testing\"\n\n"
        "// codd: covers vb=VB-CREATE-01\n"
        "func TestCreate(t *testing.T) {\n"
        "\tt.Run(\"sub\", func(t *testing.T) { if Create(2) != 3 { t.Fatalf(\"want 3\") } })\n"
        "}\n",
    )
    stream = _go_jsonl(
        [
            _go_event("run", pkg, "TestCreate"),
            _go_event("run", pkg, "TestCreate/sub"),
            _go_event("pass", pkg, "TestCreate/sub", Elapsed=0),
            _go_event("pass", pkg, "TestCreate", Elapsed=0),
            _go_event("pass", pkg, "TestGet", Elapsed=0),
            _go_event("pass", pkg, Elapsed=0.01),
        ]
    )
    report = project / ".codd" / "verify" / "go-test.jsonl"
    _write(report, stream)
    execution = GoTestJsonReportAdapter().parse(report, project_root=project)
    assert "internal/store/create_test.go" in execution.executed_passed_files
    # The parent func case is recorded; the subtest folded into it (no separate key).
    assert "internal/store/create_test.go::TestCreate" in execution.executed_passed_cases


def test_go_adapter_identity_normalization_pairs_with_static_block(tmp_path):
    """normalize_runner_identity maps a runner (Package, Test) to the SAME key the
    static GoTestBlockProfile produces for the block carrying the VB marker — so the
    gate can pair a runner case with the static test that bears the marker."""
    from codd.vb_marker_authenticity import GoTestBlockProfile

    project = _go_project(tmp_path)
    pkg = "example.com/m/internal/store"
    adapter = GoTestJsonReportAdapter()
    # Runner side: a subtest case folds to its parent func; the dir is module-relative.
    runner_key = adapter.normalize_runner_identity(pkg, "TestCreate/sub", module_path="example.com/m")
    assert runner_key == "internal/store::TestCreate"
    # Static side: GoTestBlockProfile's top-level block label for the same func, made
    # into the same "<reldir>::<TestFunc>" key the runner identity uses.
    text = (project / "internal" / "store" / "create_test.go").read_text(encoding="utf-8")
    blocks = GoTestBlockProfile().parse_test_blocks(text)
    top_level_labels = {b.label for b in blocks if "/" not in b.label}
    assert "TestCreate" in top_level_labels
    static_key = "internal/store::TestCreate"
    assert runner_key == static_key
    # An external/std package (not under the module prefix) does NOT relativize — its
    # identity is keyed by the bare package, never spuriously paired with our files.
    assert adapter.normalize_runner_identity("fmt", "TestX", module_path="example.com/m") == "fmt::TestX"


def test_go_adapter_without_go_mod_fails_closed(tmp_path):
    """ANTI-FALSE-GREEN (fail-closed): with NO go.mod, the package import-path cannot
    be relativized to a directory, so NO file is credited as passed — a passing test
    whose package cannot be mapped is NOT a silent green (it reads as not-executed,
    and the upstream empty-report CampaignError catches a wholly-unmappable run)."""
    project = tmp_path
    # No go.mod written.
    _write(
        project / "internal" / "store" / "create_test.go",
        "package store\n\nimport \"testing\"\n\n"
        "func TestCreate(t *testing.T) { if 1 != 2 { t.Fatal(\"x\") } }\n",
    )
    pkg = "example.com/m/internal/store"
    stream = _go_jsonl(
        [_go_event("pass", pkg, "TestCreate", Elapsed=0), _go_event("pass", pkg, Elapsed=0.01)]
    )
    report = project / ".codd" / "verify" / "go-test.jsonl"
    _write(report, stream)
    execution = GoTestJsonReportAdapter().parse(report, project_root=project)
    # Cannot map example.com/m/... to a dir → nothing credited (fail-closed).
    assert execution.executed_passed_files == frozenset()
    assert execution.total_cases == 0


def test_go_adapter_tolerates_nonjson_lines_but_not_empty(tmp_path):
    """Non-JSON build-noise lines are tolerated (skip-parse); a wholly non-JSON /
    empty report is unreadable (RunnerReportUnsupported), never an empty pass."""
    project = _go_project(tmp_path)
    pkg = "example.com/m/internal/store"
    noisy = (
        "# example.com/m/internal/store\n"  # raw build header (non-JSON) — tolerated
        + json.dumps(_go_event("pass", pkg, "TestGet", Elapsed=0))
        + "\n"
        + json.dumps(_go_event("pass", pkg, Elapsed=0.01))
        + "\n"
    )
    report = project / ".codd" / "verify" / "go-test.jsonl"
    _write(report, noisy)
    execution = GoTestJsonReportAdapter().parse(report, project_root=project)
    assert "internal/store/get_test.go" in execution.executed_passed_files
    # A wholly non-JSON report → unreadable (observability error, not empty pass).
    bad = project / ".codd" / "verify" / "bad.jsonl"
    _write(bad, "not json at all\nstill not json\n")
    with pytest.raises(RunnerReportUnsupported):
        GoTestJsonReportAdapter().parse(bad, project_root=project)
    # A missing report file → unreadable too.
    with pytest.raises(RunnerReportUnsupported):
        GoTestJsonReportAdapter().parse(project / "nope.jsonl", project_root=project)


def test_go_adapter_full_clean_run_verifies_all_vbs(tmp_path):
    """A go-test-json run where every VB test ran + passed → the coherence gate is
    GREEN (the positive control mirroring the vitest happy path)."""
    project = _go_project(tmp_path)
    # Remove the skip so all three VB tests can pass cleanly.
    _write(
        project / "internal" / "store" / "create_test.go",
        "package store\n\nimport \"testing\"\n\n"
        "// codd: covers vb=VB-CREATE-01\n"
        "func TestCreate(t *testing.T) { if Create(1) != 2 { t.Fatalf(\"want 2\") } }\n",
    )
    _write(
        project / "internal" / "store" / "get_test.go",
        "package store\n\nimport \"testing\"\n\n"
        "// codd: covers vb=VB-GET-01\n"
        "func TestGet(t *testing.T) { if Get(5) != 5 { t.Fatalf(\"want 5\") } }\n",
    )
    # VB-SKIP-01 no longer has a covering test, so drop it from the VB table too.
    _write(
        project / "docs" / "test" / "test_strategy.md",
        "| VB | Description |\n| --- | --- |\n"
        "| VB-CREATE-01 | create increments |\n| VB-GET-01 | get echoes |\n",
    )
    profile = _go_profile(project)
    pkg = "example.com/m/internal/store"
    stream = _go_jsonl(
        [
            _go_event("pass", pkg, "TestCreate", Elapsed=0),
            _go_event("pass", pkg, "TestGet", Elapsed=0),
            _go_event("pass", pkg, Elapsed=0.01),
        ]
    )
    report = project / ".codd" / "verify" / "go-test.jsonl"
    _write(report, stream)
    execution = GoTestJsonReportAdapter().parse(report, project_root=project)
    report_obj = build_coherence_report(project, profile=profile, execution=execution)
    assert report_obj.passed is True, [v.message for v in report_obj.unverified_vbs]
    enforce_campaign_clean_execution(execution, 0)  # no raise
