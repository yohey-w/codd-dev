---
codd:
  node_id: design:extract:verifier
  type: design
  source: extracted
  confidence: 0.65
  last_extracted: '2026-03-30'
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






## Import Dependencies

### → config

- `from codd.config import load_project_config`

## External Dependencies

- `shlex`

## Files

- `codd/verifier.py`

