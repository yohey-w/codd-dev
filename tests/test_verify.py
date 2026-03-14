"""Tests for codd verify."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from click.testing import CliRunner
from unittest.mock import patch

import pytest
import yaml

import codd.verifier as verifier_module
from codd.cli import main
from codd.verifier import (
    DEFAULT_VERIFY_CONFIG,
    DesignRef,
    TestResult as VerifyTestResult,
    TypecheckResult as VerifyTypecheckResult,
    VerifyPreflightError,
    VerifyResult,
    _Verifier,
    run_verify,
)


def _setup_project(tmp_path: Path, *, include_package_json: bool = True) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()

    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "typescript"},
                "verify": {
                    "typecheck_command": "npx tsc --noEmit",
                    "test_command": "npx jest --ci --json --outputFile=.codd/test-results.json",
                    "test_output_file": ".codd/test-results.json",
                    "report_output": "docs/test/verify_report.md",
                    "test_pattern": "tests/unit/sprint_{sprint}/**/*.test.ts",
                },
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    if include_package_json:
        (project / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
    (project / "tsconfig.json").write_text('{"compilerOptions":{"noEmit":true}}\n', encoding="utf-8")
    (project / "node_modules").mkdir()

    source_path = project / "src" / "generated" / "sprint_1" / "authentication" / "src" / "shared" / "types.ts"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(
        "\n".join(
            [
                "// @generated-by: codd implement",
                "// @generated-from: docs/design/auth_authorization_design.md (design:auth-authorization-design)",
                "",
                "export const ROLE_VALUES = ['central_admin', 'tenant_admin', 'learner'] as const;",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    test_path = project / "tests" / "unit" / "sprint_1" / "types.test.ts"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(
        "\n".join(
            [
                "import { describe, expect, it } from '@jest/globals';",
                "import { ROLE_VALUES } from '../../../src/generated/sprint_1/authentication/src/shared/types';",
                "",
                "describe('shared types', () => {",
                "  it('exports canonical role values', () => {",
                "    expect(ROLE_VALUES).toContain('central_admin');",
                "  });",
                "});",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    return project, source_path, test_path


def _write_jest_output(
    output_path: Path,
    *,
    success: bool,
    test_file_path: Path,
    failed_assertion: bool = False,
) -> None:
    payload = {
        "success": success,
        "numTotalTests": 1,
        "numPassedTests": 0 if failed_assertion else 1,
        "numFailedTests": 1 if failed_assertion else 0,
        "numPendingTests": 0,
        "testResults": [
            {
                "status": "failed" if failed_assertion else "passed",
                "name": str(test_file_path),
                "assertionResults": [
                    {
                        "status": "failed" if failed_assertion else "passed",
                        "fullName": "shared types exports canonical role values",
                        "failureMessages": ["Expected: wrong_value\nReceived: central_admin"]
                        if failed_assertion
                        else [],
                    }
                ],
            }
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload), encoding="utf-8")


def test_run_verify_pass(tmp_path):
    project, _, test_path = _setup_project(tmp_path)

    def fake_run(command, *, cwd, env, capture_output, text):
        assert cwd == str(project)
        assert capture_output is True
        assert text is True
        assert env["PATH"]
        if command[:2] == ["npx", "tsc"]:
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
        if command[:2] == ["npx", "jest"]:
            assert command[-1] == "--testPathPattern=tests/unit/sprint_1/**/*.test.ts"
            _write_jest_output(project / ".codd" / "test-results.json", success=True, test_file_path=test_path)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    with patch.object(verifier_module.subprocess, "run", side_effect=fake_run):
        result = run_verify(project, sprint=1)

    assert result.success is True
    assert result.typecheck.success is True
    assert result.typecheck.error_count == 0
    assert result.tests.success is True
    assert result.tests.passed == 1
    assert result.tests.failed == 0
    assert result.design_refs == ()
    assert result.warnings == ()
    report = Path(result.report_path).read_text(encoding="utf-8")
    assert "## Result: PASS" in report
    assert "## Tests" in report


def test_run_verify_typecheck_fail(tmp_path):
    project, source_path, test_path = _setup_project(tmp_path)

    def fake_run(command, *, cwd, env, capture_output, text):
        assert cwd == str(project)
        assert env["PATH"]
        if command[:2] == ["npx", "tsc"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=2,
                stdout="",
                stderr=(
                    "src/generated/sprint_1/authentication/src/shared/types.ts(12,3): "
                    "error TS2345: Argument of type 'number' is not assignable to parameter of type 'string'."
                ),
            )
        if command[:2] == ["npx", "jest"]:
            _write_jest_output(project / ".codd" / "test-results.json", success=True, test_file_path=test_path)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    with patch.object(verifier_module.subprocess, "run", side_effect=fake_run):
        result = run_verify(project, sprint=1)

    assert result.success is False
    assert result.typecheck.success is False
    assert result.typecheck.error_count == 1
    assert result.typecheck.errors[0].code == "TS2345"
    assert result.typecheck.errors[0].file_path == "src/generated/sprint_1/authentication/src/shared/types.ts"
    assert result.tests.success is True
    assert len(result.design_refs) == 1
    assert result.design_refs[0].node_id == "design:auth-authorization-design"
    assert result.design_refs[0].doc_path == "docs/design/auth_authorization_design.md"
    assert result.design_refs[0].source_file == str(source_path)
    assert result.design_refs[0].trace_source == "typecheck_error"


def test_run_verify_test_fail(tmp_path):
    project, source_path, test_path = _setup_project(tmp_path)

    def fake_run(command, *, cwd, env, capture_output, text):
        assert cwd == str(project)
        assert env["PATH"]
        if command[:2] == ["npx", "tsc"]:
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")
        if command[:2] == ["npx", "jest"]:
            _write_jest_output(
                project / ".codd" / "test-results.json",
                success=False,
                test_file_path=test_path,
                failed_assertion=True,
            )
            return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    with patch.object(verifier_module.subprocess, "run", side_effect=fake_run):
        result = run_verify(project, sprint=1)

    assert result.success is False
    assert result.typecheck.success is True
    assert result.tests.success is False
    assert result.tests.failed == 1
    assert len(result.tests.failures) == 1
    assert result.tests.failures[0].test_file_path == str(test_path)
    assert len(result.design_refs) == 1
    assert result.design_refs[0].source_file == str(source_path)
    assert result.design_refs[0].trace_source == "test_failure"
    report = Path(result.report_path).read_text(encoding="utf-8")
    assert "## Design Documents to Review" in report
    assert "## Suggested Propagation Targets" in report
    assert "`design:auth-authorization-design`" in report
    assert "shared types exports canonical role values" in report


def test_preflight_check_missing_package_json(tmp_path):
    project, _, _ = _setup_project(tmp_path, include_package_json=False)

    with pytest.raises(VerifyPreflightError, match="package.json"):
        run_verify(project)


def test_extract_design_refs(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    ts_file = project / "src" / "example.ts"
    ts_file.parent.mkdir(parents=True, exist_ok=True)
    ts_file.write_text(
        "\n".join(
            [
                "// @generated-from: docs/design/system_design.md (design:system-design)",
                "export const ready = true;",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    verifier = _Verifier(project, dict(DEFAULT_VERIFY_CONFIG))
    refs, warnings = verifier._extract_design_refs(ts_file, "typecheck_error")

    assert warnings == []
    assert len(refs) == 1
    assert refs[0].node_id == "design:system-design"
    assert refs[0].doc_path == "docs/design/system_design.md"
    assert refs[0].source_file == str(ts_file)


def test_extract_design_refs_missing_header(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    ts_file = project / "src" / "manual.ts"
    ts_file.parent.mkdir(parents=True, exist_ok=True)
    ts_file.write_text("export const ready = true;\n", encoding="utf-8")

    verifier = _Verifier(project, dict(DEFAULT_VERIFY_CONFIG))
    refs, warnings = verifier._extract_design_refs(ts_file, "test_failure")

    assert refs == []
    assert len(warnings) == 1
    assert str(ts_file) in warnings[0]


def test_verify_cli_reports_propagate_targets(tmp_path):
    project = tmp_path / "project"
    (project / "codd").mkdir(parents=True)

    result = VerifyResult(
        success=False,
        typecheck=VerifyTypecheckResult(success=True, error_count=0, errors=()),
        tests=VerifyTestResult(
            success=False,
            total=1,
            passed=0,
            failed=1,
            skipped=0,
            failures=(),
        ),
        design_refs=(
            DesignRef(
                node_id="design:shared-domain-model",
                doc_path="docs/detailed_design/shared_domain_model.md",
                trace_source="test_failure",
                source_file=str(project / "src" / "generated" / "types.ts"),
            ),
            DesignRef(
                node_id="design:shared-domain-model",
                doc_path="docs/detailed_design/shared_domain_model.md",
                trace_source="test_failure",
                source_file=str(project / "src" / "generated" / "types.ts"),
            ),
            DesignRef(
                node_id="design:auth-authorization-design",
                doc_path="docs/design/auth_authorization_design.md",
                trace_source="test_failure",
                source_file=str(project / "src" / "generated" / "types.ts"),
            ),
        ),
        warnings=(),
        report_path=str(project / "docs" / "test" / "verify_report.md"),
    )

    runner = CliRunner()
    with patch("codd.verifier.run_verify", return_value=result):
        cli_result = runner.invoke(main, ["verify", "--path", str(project), "--sprint", "1"])

    assert cli_result.exit_code == 1
    assert "Suggested propagate targets:" in cli_result.output
    assert "design:shared-domain-model" in cli_result.output
    assert "design:auth-authorization-design" in cli_result.output
    assert (
        "Suggested propagate targets:\n"
        "  design:shared-domain-model\n"
        "  design:auth-authorization-design"
    ) in cli_result.output
