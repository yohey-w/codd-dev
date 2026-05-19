---
codd:
  node_id: "design:verify-architecture"
  type: design
  depends_on:
    - id: "req:extract-verify"
      relation: implements
    - id: "design:extract-architecture"
      relation: derives_from
---

# Verify Architecture Design

## Overview

`codd verify` runs build+test verification and traces failures back to design documents. The current implementation is hardcoded to TypeScript/Node.js. This design refactors it into a language-agnostic architecture with Python as first-class citizen.

## Current State (TypeScript-only)

- Preflight: Requires package.json, tsconfig.json, node_modules
- Typecheck: `npx tsc --noEmit` with TSC error regex
- Test: `npx jest --ci --json` with Jest JSON parsing
- Design tracing: `@generated-from` comment regex (TS imports)

## Target Architecture

### Language Strategy Pattern

```python
class LanguageVerifyStrategy(Protocol):
    def preflight(self, project_root: Path) -> None: ...
    def run_typecheck(self, project_root: Path, config: dict) -> TypecheckResult: ...
    def run_tests(self, project_root: Path, config: dict, sprint: int | None) -> TestResult: ...
    def collect_design_refs(self, project_root: Path, failures: ...) -> tuple[DesignRef, ...]: ...
```

### Strategy Implementations

| Language | Strategy | Typecheck | Test Runner |
|----------|----------|-----------|-------------|
| python | PythonVerifyStrategy | mypy (default) or pyright | pytest |
| typescript | TypeScriptVerifyStrategy | tsc | jest |
| go | GoVerifyStrategy (future) | go vet | go test |

### PythonVerifyStrategy

#### Preflight
- Check for: `pyproject.toml` OR `setup.py` OR `setup.cfg`
- Optional: Check mypy/pyright availability

#### Typecheck
- Default command: `mypy .` (configurable via `verify.typecheck_command`)
- Error regex: `^(.+):(\d+): error: (.+) \[(.+)\]$` (mypy format)
- Alternative: `pyright` output parsing

#### Test Runner
- Default command: `pytest --tb=short -q` (configurable via `verify.test_command`)
- Parse pytest output: collect test counts and failure details
- Sprint filtering: `pytest tests/sprint_{sprint}/` pattern

#### Design Traceability
- Python `@generated-from` comment: `# @generated-from: path (node_id)`
- Import-based tracing: map test failures to source modules → design docs

### Configuration (codd.yaml)

```yaml
verify:
  # Language auto-detected from project.language, or override:
  typecheck_command: "mypy codd/"
  test_command: "pytest --tb=short -q"
  test_output_file: ".codd/test-results.json"
  report_output: "docs/test/verify_report.md"
```

Defaults per language loaded from `DEFAULT_VERIFY_CONFIGS[language]`.

### Runtime Smoke CRUD Flow Category

`codd verify --runtime` extends Step 8 with an opt-in `crud-flow` category. The
existing DB, dev-server, smoke connectivity, and real-browser E2E checks remain
unchanged. A project enables CRUD reflection checks only by declaring
`runtime.crud_flow_targets`:

```yaml
runtime:
  crud_flow_targets:
    - name: "create item appears in list"
      command: "npx playwright test tests/smoke/create-item.spec.ts"
    - name: "api create then list reflects"
      create:
        method: POST
        url: "/api/items"
        expected_status: 201
        json: {name: "codd-runtime-smoke"}
      reflect:
        url: "/items"
        expected_status: 200
        expect_text: "codd-runtime-smoke"
      max_wait_seconds: 10
      poll_interval: 0.5
```

Execution rules:
- Command targets run as project-owned tests and pass on exit code 0.
- Declarative targets issue the mutating request, then poll the reflection URL
  until the expected status and optional text are observed.
- Missing `runtime.crud_flow_targets` is a no-op for backward compatibility.
- `--runtime-skip crud-flow` records the category as skipped in the runtime report.

### Doctor Warning

`codd doctor` performs a lightweight static diagnostic:

1. Scan configured source directories for POST-like handlers.
2. If found, check for `runtime.crud_flow_targets`.
3. If absent, scan configured test directories for POST tests with reflection
   markers such as list, reload, visible, locator, or expectation assertions.
4. Emit a warning when mutating endpoints exist without a reflection-oriented
   runtime check.

This is a warning only. It is intentionally heuristic and framework-agnostic; it
does not block existing projects.

### Strategy Selection

```python
def _get_verify_strategy(language: str) -> LanguageVerifyStrategy:
    strategies = {
        "python": PythonVerifyStrategy,
        "typescript": TypeScriptVerifyStrategy,
        "javascript": TypeScriptVerifyStrategy,
    }
    return strategies.get(language, TypeScriptVerifyStrategy)()
```

Language comes from `codd.yaml` → `project.language`.

## Verification Report Format (unchanged)

```markdown
# CoDD Verification Report
Generated: {timestamp}

## Typecheck
{pass/fail, error count, error details}

## Tests
{pass/fail, total/passed/failed/skipped, failure details}

## Design Impact
{design_refs traced from failures}

## Suggested propagate targets:
{unique node_ids from design_refs}
```

## Files

- `codd/verifier.py` — VerifyResult, run_verify(), _Verifier, strategy implementations
