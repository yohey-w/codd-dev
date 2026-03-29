---
codd:
  node_id: design:extract:verifier
  type: design
  source: extracted
  confidence: 0.65
  last_extracted: '2026-03-30'
  source_files:
  - codd/verifier.py
  depends_on:
  - id: design:extract:config
    relation: imports
    semantic: technical
---
# verifier

> 1 files, 679 lines

**Layer Guess**: Application
**Responsibility**: Coordinates use cases or service-level workflows

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `TypecheckError` | `codd/verifier.py:24` | — |
| class | `TypecheckResult` | `codd/verifier.py:33` | — |
| class | `TestFailure` | `codd/verifier.py:43` | — |
| class | `TestResult` | `codd/verifier.py:50` | — |
| class | `DesignRef` | `codd/verifier.py:63` | — |
| class | `VerifyResult` | `codd/verifier.py:71` | — |
| class | `VerifyPreflightError` | `codd/verifier.py:80` | bases: Exception |
| class | `_Verifier` | `codd/verifier.py:212` | — |
| function | `run_verify` | `codd/verifier.py:183` | `run_verify(project_root: Path, sprint: int | None = None,) -> VerifyResult` |
| function | `run` | `codd/verifier.py:218` | `run(self, sprint: int | None = None) -> VerifyResult` |






## Public API

- `TypecheckError`
- `TypecheckResult`
- `TestFailure`
- `TestResult`
- `DesignRef`
- `VerifyResult`
- `VerifyPreflightError`
- `run_verify`
- `_Verifier`
- `run`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `run_verify` | `_Verifier` | `codd/verifier.py:189` | no |
| `run_verify` | `run` | `codd/verifier.py:190` | no |
| `_Verifier.run` | `VerifyResult` | `codd/verifier.py:248` | no |
| `_Verifier.run` | `VerifyResult` | `codd/verifier.py:257` | no |
| `_Verifier._preflight_check` | `VerifyPreflightError` | `codd/verifier.py:281` | no |
| `_Verifier._preflight_check` | `VerifyPreflightError` | `codd/verifier.py:288` | no |
| `_Verifier._run_typecheck` | `TypecheckResult` | `codd/verifier.py:300` | no |
| `_Verifier._run_typecheck` | `run` | `codd/verifier.py:302` | no |
| `_Verifier._run_typecheck` | `TypecheckResult` | `codd/verifier.py:311` | no |
| `_Verifier._parse_tsc_typecheck` | `TypecheckError` | `codd/verifier.py:324` | no |
| `_Verifier._parse_python_typecheck` | `TypecheckError` | `codd/verifier.py:338` | no |
| `_Verifier._parse_python_typecheck` | `TypecheckError` | `codd/verifier.py:349` | no |
| `_Verifier._run_pytest` | `run` | `codd/verifier.py:374` | no |
| `_Verifier._parse_pytest_output` | `TestFailure` | `codd/verifier.py:387` | no |
| `_Verifier._parse_pytest_output` | `TestResult` | `codd/verifier.py:407` | no |
| `_Verifier._run_jest` | `run` | `codd/verifier.py:428` | no |
| `_Verifier._run_jest` | `TestFailure` | `codd/verifier.py:445` | no |
| `_Verifier._run_jest` | `TestResult` | `codd/verifier.py:452` | no |
| `_Verifier._extract_design_refs` | `DesignRef` | `codd/verifier.py:606` | no |

## Test Coverage

**Coverage**: 0.0 (0 / 10)

**Uncovered symbols**: `DesignRef`, `TestFailure`, `TestResult`, `TypecheckError`, `TypecheckResult`, `VerifyPreflightError`, `VerifyResult`, `_Verifier`, `run`, `run_verify`


## Environment Dependencies

| Key | Kind | Location | Default |
|-----|------|----------|---------|
| `test_command` | config | `codd/verifier.py:423` | no |


## Import Dependencies

### → config

- `from codd.config import load_project_config`

## External Dependencies

- `shlex`

## Files

- `codd/verifier.py`

