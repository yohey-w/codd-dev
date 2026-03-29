"""CoDD verifier for typecheck and test validation with design traceability.

Supports Python and TypeScript/JavaScript projects. Language is detected from
codd.yaml project.language, with per-language defaults for typecheck and test
commands.
"""

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


VerifyTypecheckResult = TypecheckResult  # alias for test compat


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


VerifyTestResult = TestResult  # alias for test compat


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


# ---------------------------------------------------------------------------
# Per-language default configurations
# ---------------------------------------------------------------------------

_PYTHON_DEFAULTS: dict[str, Any] = {
    "typecheck_command": "mypy .",
    "test_command": "pytest --tb=short -q",
    "report_output": "docs/test/verify_report.md",
    "preflight_files": ["pyproject.toml", "setup.py", "setup.cfg"],
    "preflight_mode": "any",  # at least one must exist
}

_TYPESCRIPT_DEFAULTS: dict[str, Any] = {
    "typecheck_command": "npx tsc --noEmit",
    "test_command": "npx jest --ci --json --outputFile=.codd/test-results.json",
    "test_output_file": ".codd/test-results.json",
    "report_output": "docs/test/verify_report.md",
    "test_pattern": "tests/unit/sprint_{sprint}/**/*.test.ts",
    "preflight_files": ["package.json", "tsconfig.json", "node_modules"],
    "preflight_mode": "all",  # all must exist
}

DEFAULT_VERIFY_CONFIGS: dict[str, dict[str, Any]] = {
    "python": _PYTHON_DEFAULTS,
    "typescript": _TYPESCRIPT_DEFAULTS,
    "javascript": {
        **_TYPESCRIPT_DEFAULTS,
        "typecheck_command": "",
        "preflight_files": ["package.json", "node_modules"],
    },
}

# Keep old name for backwards compat in tests
DEFAULT_VERIFY_CONFIG = _TYPESCRIPT_DEFAULTS

# ---------------------------------------------------------------------------
# Error / output regexes
# ---------------------------------------------------------------------------

# TypeScript: file(line,col): error TSxxxx: message
TSC_ERROR_RE = re.compile(
    r"^(?P<file>[^(]+)\((?P<line>\d+),(?P<col>\d+)\):\s*error\s+(?P<code>TS\d+):\s*(?P<message>.+)$",
    re.MULTILINE,
)

# mypy: file.py:line: error: message  [code]
MYPY_ERROR_RE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):\s*error:\s*(?P<message>.+?)\s*\[(?P<code>[^\]]+)\]\s*$",
    re.MULTILINE,
)

# pyright: file.py:line:col - error: message (code)
PYRIGHT_ERROR_RE = re.compile(
    r"^\s*(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)\s*-\s*error:\s*(?P<message>.+?)\s*\((?P<code>[^)]+)\)\s*$",
    re.MULTILINE,
)

# pytest FAILED line: FAILED tests/test_foo.py::test_bar - reason
PYTEST_FAILED_RE = re.compile(
    r"^FAILED\s+(?P<file>[^:]+)::(?P<test>\S+?)(?:\s+-\s+(?P<message>.+))?$",
    re.MULTILINE,
)

# pytest summary: "1 failed, 126 passed, 2 skipped in 1.10s"
PYTEST_SUMMARY_RE = re.compile(
    r"=+\s*(?P<summary>[^=]+?)\s*=+\s*$",
    re.MULTILINE,
)

# Design traceability comments
# TypeScript: // @generated-from: path (node_id)
TS_GENERATED_FROM_RE = re.compile(
    r"^//\s*@generated-from:\s*(?P<path>.+?)\s*\((?P<node_id>[^)]+)\)\s*$",
    re.MULTILINE,
)
# Python: # @generated-from: path (node_id)
PY_GENERATED_FROM_RE = re.compile(
    r"^#\s*@generated-from:\s*(?P<path>.+?)\s*\((?P<node_id>[^)]+)\)\s*$",
    re.MULTILINE,
)

# Keep old name for backwards compat in tests
GENERATED_FROM_RE = TS_GENERATED_FROM_RE

# Import regexes for design ref tracing
TS_IMPORT_RE = re.compile(
    r"^\s*import\s+.*?\s+from\s+['\"](?P<path>[^'\"]+)['\"]",
    re.MULTILINE,
)
PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(?P<from>[^\s]+)\s+import|import\s+(?P<mod>[^\s,]+))",
    re.MULTILINE,
)


def _propagate_targets(design_refs: tuple[DesignRef, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref.node_id for ref in design_refs))


def run_verify(
    project_root: Path,
    sprint: int | None = None,
) -> VerifyResult:
    """Run build + test verification and trace failures to design documents."""
    config = _load_verify_config(project_root)
    verifier = _Verifier(project_root.resolve(), config)
    return verifier.run(sprint)


def _load_verify_config(project_root: Path) -> dict[str, Any]:
    """Load merged CoDD config with language-appropriate defaults."""
    project_config = load_project_config(project_root.resolve())
    language = (project_config.get("project") or {}).get("language", "typescript")

    defaults = dict(DEFAULT_VERIFY_CONFIGS.get(language, _TYPESCRIPT_DEFAULTS))
    defaults["_language"] = language

    verify_overrides = project_config.get("verify") or {}
    if not isinstance(verify_overrides, dict):
        raise ValueError("codd verify config must be a mapping")
    defaults.update(verify_overrides)
    return defaults


# Keep old name for backwards compat in tests
_load_project_config = _load_verify_config


class _Verifier:
    def __init__(self, project_root: Path, config: dict[str, Any]):
        self.project_root = project_root
        self.config = config
        self.language = config.get("_language", "typescript")

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

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    def _preflight_check(self) -> None:
        preflight_files = self.config.get("preflight_files", [])
        if not preflight_files:
            return

        mode = self.config.get("preflight_mode", "all")
        missing = [f for f in preflight_files if not (self.project_root / f).exists()]

        if mode == "any":
            # At least one must exist
            if len(missing) == len(preflight_files):
                raise VerifyPreflightError(
                    f"Preflight check failed: None of {', '.join(preflight_files)} found. "
                    f"Is this a {self.language} project?"
                )
        else:
            # All must exist
            if missing:
                raise VerifyPreflightError(
                    f"Preflight check failed: Missing: {', '.join(missing)}. "
                    f"Run setup first."
                )

    # ------------------------------------------------------------------
    # Typecheck
    # ------------------------------------------------------------------

    def _run_typecheck(self) -> TypecheckResult:
        command = self.config.get("typecheck_command", "")
        if not command:
            return TypecheckResult(success=True, error_count=0, errors=())

        proc = subprocess.run(
            shlex.split(command),
            cwd=str(self.project_root),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )
        output = proc.stdout + proc.stderr
        errors = self._parse_typecheck_errors(output)
        return TypecheckResult(
            success=proc.returncode == 0,
            error_count=len(errors),
            errors=errors,
        )

    def _parse_typecheck_errors(self, output: str) -> tuple[TypecheckError, ...]:
        if self.language == "python":
            return self._parse_python_typecheck(output)
        return self._parse_tsc_typecheck(output)

    def _parse_tsc_typecheck(self, output: str) -> tuple[TypecheckError, ...]:
        return tuple(
            TypecheckError(
                file_path=m.group("file").strip(),
                line=int(m.group("line")),
                col=int(m.group("col")),
                code=m.group("code"),
                message=m.group("message").strip(),
            )
            for m in TSC_ERROR_RE.finditer(output)
        )

    def _parse_python_typecheck(self, output: str) -> tuple[TypecheckError, ...]:
        # Try mypy first, then pyright
        errors: list[TypecheckError] = []
        for m in MYPY_ERROR_RE.finditer(output):
            errors.append(TypecheckError(
                file_path=m.group("file").strip(),
                line=int(m.group("line")),
                col=0,
                code=m.group("code"),
                message=m.group("message").strip(),
            ))
        if errors:
            return tuple(errors)

        for m in PYRIGHT_ERROR_RE.finditer(output):
            errors.append(TypecheckError(
                file_path=m.group("file").strip(),
                line=int(m.group("line")),
                col=int(m.group("col")),
                code=m.group("code"),
                message=m.group("message").strip(),
            ))
        return tuple(errors)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def _run_tests(self, sprint: int | None) -> TestResult:
        if self.language == "python":
            return self._run_pytest(sprint)
        return self._run_jest(sprint)

    def _run_pytest(self, sprint: int | None) -> TestResult:
        command = shlex.split(self.config.get("test_command", "pytest --tb=short -q"))

        if sprint is not None:
            pattern = self.config.get("test_pattern", "tests/sprint_{sprint}/")
            command.append(pattern.format(sprint=sprint))

        proc = subprocess.run(
            command,
            cwd=str(self.project_root),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )
        output = proc.stdout + proc.stderr
        return self._parse_pytest_output(output, proc.returncode)

    def _parse_pytest_output(self, output: str, returncode: int) -> TestResult:
        failures: list[TestFailure] = []
        for m in PYTEST_FAILED_RE.finditer(output):
            failures.append(TestFailure(
                test_file_path=m.group("file"),
                test_name=m.group("test"),
                failure_messages=(m.group("message") or "",),
            ))

        # Parse summary line: "1 failed, 126 passed, 2 skipped in 1.10s"
        passed = failed = skipped = 0
        total_match = re.search(r"(\d+)\s+passed", output)
        if total_match:
            passed = int(total_match.group(1))
        fail_match = re.search(r"(\d+)\s+failed", output)
        if fail_match:
            failed = int(fail_match.group(1))
        skip_match = re.search(r"(\d+)\s+skipped", output)
        if skip_match:
            skipped = int(skip_match.group(1))

        total = passed + failed + skipped

        return TestResult(
            success=(returncode == 0),
            total=total,
            passed=passed,
            failed=failed,
            skipped=skipped,
            failures=tuple(failures),
        )

    def _run_jest(self, sprint: int | None) -> TestResult:
        output_path = self._resolve_path(
            self.config.get("test_output_file", ".codd/test-results.json")
        )
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
        data = self._load_jest_output(output_path, proc.stdout)

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

    def _load_jest_output(self, output_path: Path, stdout: str) -> dict[str, Any]:
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

    # ------------------------------------------------------------------
    # Design traceability
    # ------------------------------------------------------------------

    def _trace_from_typecheck_errors(
        self, errors: tuple[TypecheckError, ...]
    ) -> tuple[list[DesignRef], list[str]]:
        refs: list[DesignRef] = []
        warnings: list[str] = []
        for error in errors:
            src_file = Path(error.file_path)
            if not src_file.is_absolute():
                src_file = self.project_root / src_file
            new_refs, new_warnings = self._extract_design_refs(src_file, "typecheck_error")
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

            if self.language == "python":
                self._trace_python_imports(content, test_file, refs, warnings)
            else:
                self._trace_ts_imports(content, test_file, refs, warnings)
        return refs, warnings

    def _trace_python_imports(
        self, content: str, test_file: Path,
        refs: list[DesignRef], warnings: list[str],
    ) -> None:
        for m in PY_IMPORT_RE.finditer(content):
            mod_path = m.group("from") or m.group("mod") or ""
            if not mod_path:
                continue
            # Convert dotted module to file path
            parts = mod_path.replace(".", "/")
            for suffix in (".py", "/__init__.py"):
                candidate = self.project_root / (parts + suffix)
                if candidate.exists():
                    new_refs, new_warnings = self._extract_design_refs(candidate, "test_failure")
                    refs.extend(new_refs)
                    warnings.extend(new_warnings)
                    break

    def _trace_ts_imports(
        self, content: str, test_file: Path,
        refs: list[DesignRef], warnings: list[str],
    ) -> None:
        for import_match in TS_IMPORT_RE.finditer(content):
            import_path = import_match.group("path")
            if not import_path.startswith("."):
                continue
            for candidate in self._resolve_ts_import_candidates(test_file, import_path):
                if not candidate.exists():
                    continue
                new_refs, new_warnings = self._extract_design_refs(candidate, "test_failure")
                refs.extend(new_refs)
                warnings.extend(new_warnings)
                break

    def _resolve_ts_import_candidates(self, test_file: Path, import_path: str) -> tuple[Path, ...]:
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
        self, source_file: Path, trace_source: str
    ) -> tuple[list[DesignRef], list[str]]:
        warnings: list[str] = []
        refs: list[DesignRef] = []
        if not source_file.exists():
            return refs, warnings

        try:
            header = "\n".join(source_file.read_text(encoding="utf-8").splitlines()[:30])
        except OSError:
            return refs, warnings

        # Use language-appropriate comment regex
        if self.language == "python":
            pattern = PY_GENERATED_FROM_RE
        else:
            pattern = TS_GENERATED_FROM_RE

        matches = list(pattern.finditer(header))
        if not matches:
            warnings.append(f"No @generated-from header in {source_file} — manual review required")
            return refs, warnings

        for match in matches:
            refs.append(
                DesignRef(
                    node_id=match.group("node_id"),
                    doc_path=match.group("path"),
                    trace_source=trace_source,
                    source_file=str(source_file),
                )
            )
        return refs, warnings

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    def _generate_report(self, result: VerifyResult) -> str:
        report_path = self._resolve_path(self.config.get("report_output", "docs/test/verify_report.md"))
        report_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            "# CoDD Verify Report",
            "",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Language: {self.language}",
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
