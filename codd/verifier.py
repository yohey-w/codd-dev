"""CoDD verifier for typecheck and test validation with design traceability."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from codd.config import load_project_config


@dataclass(frozen=True)
class TypecheckError:
    file_path: str
    line: int
    col: int
    code: str
    message: str


@dataclass(frozen=True)
class TypecheckResult:
    success: bool
    error_count: int
    errors: tuple[TypecheckError, ...]


@dataclass(frozen=True)
class TestFailure:
    test_file_path: str
    test_name: str
    failure_messages: tuple[str, ...]


@dataclass(frozen=True)
class TestResult:
    success: bool
    total: int
    passed: int
    failed: int
    skipped: int
    failures: tuple[TestFailure, ...]


@dataclass(frozen=True)
class DesignRef:
    node_id: str
    doc_path: str
    trace_source: str
    source_file: str


@dataclass(frozen=True)
class VerifyResult:
    success: bool
    typecheck: TypecheckResult
    tests: TestResult
    design_refs: tuple[DesignRef, ...]
    warnings: tuple[str, ...]
    report_path: str


class VerifyPreflightError(Exception):
    """Raised when the target project is missing required build/test inputs."""


DEFAULT_VERIFY_CONFIG: dict[str, Any] = {
    "typecheck_command": "npx tsc --noEmit",
    "test_command": "npx jest --ci --json --outputFile=.codd/test-results.json",
    "test_output_file": ".codd/test-results.json",
    "report_output": "docs/test/verify_report.md",
    "test_pattern": "tests/unit/sprint_{sprint}/**/*.test.ts",
}

GENERATED_FROM_RE = re.compile(
    r"^//\s*@generated-from:\s*(?P<path>.+?)\s*\((?P<node_id>[^)]+)\)\s*$",
    re.MULTILINE,
)
TSC_ERROR_RE = re.compile(
    r"^(?P<file>[^(]+)\((?P<line>\d+),(?P<col>\d+)\):\s*error\s+(?P<code>TS\d+):\s*(?P<message>.+)$",
    re.MULTILINE,
)
TS_IMPORT_RE = re.compile(
    r"^\s*import\s+.*?\s+from\s+['\"](?P<path>[^'\"]+)['\"]",
    re.MULTILINE,
)


def _propagate_targets(design_refs: tuple[DesignRef, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref.node_id for ref in design_refs))


def run_verify(
    project_root: Path,
    sprint: int | None = None,
) -> VerifyResult:
    """Run build + test verification and trace failures to design documents."""
    config = _load_project_config(project_root)
    verifier = _Verifier(project_root.resolve(), config)
    return verifier.run(sprint)


def _load_project_config(project_root: Path) -> dict[str, Any]:
    """Load merged CoDD config and extract the verify section."""
    config = dict(DEFAULT_VERIFY_CONFIG)
    project_config = load_project_config(project_root.resolve())
    verify_config = project_config.get("verify", {})
    if verify_config is None:
        return config
    if not isinstance(verify_config, dict):
        raise ValueError("codd verify config must be a mapping")
    config.update(verify_config)
    return config


class _Verifier:
    def __init__(self, project_root: Path, config: dict[str, Any]):
        self.project_root = project_root
        self.config = config

    def run(self, sprint: int | None = None) -> VerifyResult:
        warnings: list[str] = []
        self._preflight_check()

        (self.project_root / ".codd").mkdir(exist_ok=True)

        typecheck_result = self._run_typecheck()
        test_result = self._run_tests(sprint)

        design_refs: list[DesignRef] = []
        if not typecheck_result.success:
            refs, new_warnings = self._trace_from_typecheck_errors(typecheck_result.errors)
            design_refs.extend(refs)
            warnings.extend(new_warnings)
        if not test_result.success:
            refs, new_warnings = self._trace_from_test_failures(test_result.failures)
            design_refs.extend(refs)
            warnings.extend(new_warnings)

        unique_refs: list[DesignRef] = []
        seen_ref_keys: set[tuple[str, str]] = set()
        for ref in design_refs:
            key = (ref.node_id, ref.source_file)
            if key in seen_ref_keys:
                continue
            seen_ref_keys.add(key)
            unique_refs.append(ref)

        unique_warnings = tuple(dict.fromkeys(warnings))
        success = typecheck_result.success and test_result.success
        interim = VerifyResult(
            success=success,
            typecheck=typecheck_result,
            tests=test_result,
            design_refs=tuple(unique_refs),
            warnings=unique_warnings,
            report_path="",
        )
        report_path = self._generate_report(interim)
        return VerifyResult(
            success=success,
            typecheck=typecheck_result,
            tests=test_result,
            design_refs=tuple(unique_refs),
            warnings=unique_warnings,
            report_path=report_path,
        )

    def _preflight_check(self) -> None:
        missing = []
        for name in ("package.json", "tsconfig.json", "node_modules"):
            if not (self.project_root / name).exists():
                missing.append(name)
        if missing:
            raise VerifyPreflightError(f"Missing: {', '.join(missing)}. Run npm install first.")

    def _run_typecheck(self) -> TypecheckResult:
        proc = subprocess.run(
            shlex.split(self.config["typecheck_command"]),
            cwd=str(self.project_root),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )
        output = proc.stdout + proc.stderr
        errors = tuple(
            TypecheckError(
                file_path=match.group("file").strip(),
                line=int(match.group("line")),
                col=int(match.group("col")),
                code=match.group("code"),
                message=match.group("message").strip(),
            )
            for match in TSC_ERROR_RE.finditer(output)
        )
        return TypecheckResult(
            success=proc.returncode == 0,
            error_count=len(errors),
            errors=errors,
        )

    def _run_tests(self, sprint: int | None) -> TestResult:
        output_path = self._resolve_path(self.config.get("test_output_file", ".codd/test-results.json"))
        if output_path.exists():
            output_path.unlink()

        command = shlex.split(self.config["test_command"])
        if sprint is not None:
            pattern = self.config.get("test_pattern", "tests/unit/sprint_{sprint}/**/*.test.ts")
            command.append(f"--testPathPattern={pattern.format(sprint=sprint)}")

        proc = subprocess.run(
            command,
            cwd=str(self.project_root),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )
        data = self._load_test_output(output_path, proc.stdout)

        failures: list[TestFailure] = []
        for suite in data.get("testResults", []):
            if suite.get("status") != "failed":
                continue
            for assertion in suite.get("assertionResults", []):
                if assertion.get("status") != "failed":
                    continue
                failures.append(
                    TestFailure(
                        test_file_path=str(suite.get("testFilePath") or suite.get("name") or ""),
                        test_name=str(assertion.get("fullName", "")),
                        failure_messages=tuple(assertion.get("failureMessages", [])),
                    )
                )

        return TestResult(
            success=bool(data.get("success", False) and proc.returncode == 0),
            total=int(data.get("numTotalTests", 0)),
            passed=int(data.get("numPassedTests", 0)),
            failed=int(data.get("numFailedTests", 0)),
            skipped=int(data.get("numPendingTests", 0)),
            failures=tuple(failures),
        )

    def _load_test_output(self, output_path: Path, stdout: str) -> dict[str, Any]:
        if output_path.exists():
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("jest output must be a JSON object")
            return payload

        raw = stdout.strip()
        if raw:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("jest stdout JSON must be an object")
            return payload

        return {
            "success": False,
            "numTotalTests": 0,
            "numPassedTests": 0,
            "numFailedTests": 0,
            "numPendingTests": 0,
            "testResults": [],
        }

    def _trace_from_typecheck_errors(
        self, errors: tuple[TypecheckError, ...]
    ) -> tuple[list[DesignRef], list[str]]:
        refs: list[DesignRef] = []
        warnings: list[str] = []
        for error in errors:
            ts_file = Path(error.file_path)
            if not ts_file.is_absolute():
                ts_file = self.project_root / ts_file
            new_refs, new_warnings = self._extract_design_refs(ts_file, "typecheck_error")
            refs.extend(new_refs)
            warnings.extend(new_warnings)
        return refs, warnings

    def _trace_from_test_failures(
        self, failures: tuple[TestFailure, ...]
    ) -> tuple[list[DesignRef], list[str]]:
        refs: list[DesignRef] = []
        warnings: list[str] = []
        for failure in failures:
            test_file = Path(failure.test_file_path)
            if not test_file.is_absolute():
                test_file = self.project_root / test_file
            if not test_file.exists() or test_file.is_dir():
                continue

            content = test_file.read_text(encoding="utf-8")
            for import_match in TS_IMPORT_RE.finditer(content):
                import_path = import_match.group("path")
                if not import_path.startswith("."):
                    continue
                for candidate in self._resolve_import_candidates(test_file, import_path):
                    if not candidate.exists():
                        continue
                    new_refs, new_warnings = self._extract_design_refs(candidate, "test_failure")
                    refs.extend(new_refs)
                    warnings.extend(new_warnings)
                    break
        return refs, warnings

    def _resolve_import_candidates(self, test_file: Path, import_path: str) -> tuple[Path, ...]:
        base = (test_file.parent / import_path).resolve()
        candidates = [base]
        if base.suffix:
            candidates.extend([base.with_suffix(".ts"), base.with_suffix(".tsx")])
        else:
            candidates.extend(
                [
                    base.with_suffix(".ts"),
                    base.with_suffix(".tsx"),
                    base / "index.ts",
                    base / "index.tsx",
                ]
            )

        unique: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            unique.append(candidate)
        return tuple(unique)

    def _extract_design_refs(
        self, ts_file: Path, trace_source: str
    ) -> tuple[list[DesignRef], list[str]]:
        warnings: list[str] = []
        refs: list[DesignRef] = []
        if not ts_file.exists():
            return refs, warnings

        try:
            header = "\n".join(ts_file.read_text(encoding="utf-8").splitlines()[:30])
        except OSError:
            return refs, warnings

        matches = list(GENERATED_FROM_RE.finditer(header))
        if not matches:
            warnings.append(f"No @generated-from header in {ts_file} — manual review required")
            return refs, warnings

        for match in matches:
            refs.append(
                DesignRef(
                    node_id=match.group("node_id"),
                    doc_path=match.group("path"),
                    trace_source=trace_source,
                    source_file=str(ts_file),
                )
            )
        return refs, warnings

    def _generate_report(self, result: VerifyResult) -> str:
        report_path = self._resolve_path(self.config.get("report_output", "docs/test/verify_report.md"))
        report_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# CoDD Verify Report",
            "",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"## Result: {'PASS' if result.success else 'FAIL'}",
            "",
            "## Typecheck",
            f"- Status: {'PASS' if result.typecheck.success else 'FAIL'}",
            f"- Errors: {result.typecheck.error_count}",
            "",
            "## Tests",
            f"- Status: {'PASS' if result.tests.success else 'FAIL'}",
            (
                f"- Total: {result.tests.total} | Passed: {result.tests.passed} | "
                f"Failed: {result.tests.failed} | Skipped: {result.tests.skipped}"
            ),
        ]

        if result.typecheck.errors:
            lines.extend(["", "### Typecheck Errors"])
            for error in result.typecheck.errors:
                lines.append(
                    f"- `{error.file_path}:{error.line}:{error.col}` {error.code}: {error.message}"
                )

        if result.tests.failures:
            lines.extend(["", "### Test Failures"])
            for failure in result.tests.failures:
                lines.append(f"- `{failure.test_file_path}` - {failure.test_name}")

        if result.design_refs:
            lines.extend(["", "## Design Documents to Review"])
            for ref in result.design_refs:
                lines.append(
                    f"- **{ref.node_id}** -> `{ref.doc_path}` (from `{ref.source_file}`, via {ref.trace_source})"
                )
            targets = _propagate_targets(result.design_refs)
            if targets:
                lines.extend(["", "## Suggested Propagation Targets"])
                for target in targets:
                    lines.append(f"- `{target}`")

        if result.warnings:
            lines.extend(["", "## Warnings"])
            for warning in result.warnings:
                lines.append(f"- {warning}")

        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(report_path)

    def _resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        return self.project_root / path
