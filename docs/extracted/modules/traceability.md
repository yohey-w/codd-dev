---
codd:
  node_id: design:extract:traceability
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/traceability.py
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# traceability

> 1 files, 67 lines

**Layer Guess**: Infrastructure
**Responsibility**: Defaulted to infrastructure because no higher-level cues were detected

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `TestCoverage` | `codd/traceability.py:18` | — |
| function | `build_test_traceability` | `codd/traceability.py:27` | `build_test_traceability(facts: ProjectFacts, project_root: Path) -> None` |






## Public API

- `TestCoverage`
- `build_test_traceability`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `build_test_traceability` | `TestCoverage` | `codd/traceability.py:61` | no |

## Test Coverage

**Coverage**: 1.0 (2 / 2)
Tests: tests/test_traceability.py



## Import Dependencies

### → extractor

- `from codd.extractor import ProjectFacts`


## Files

- `codd/traceability.py`

## Tests

- `tests/test_traceability.py` — tests: test_test_coverage_dataclass_defaults, test_build_test_traceability_all_covered, test_login, test_logout, test_build_test_traceability_partial_coverage, test_build_test_traceability_no_test_files, test_build_test_traceability_no_symbols, test_build_test_traceability_missing_test_file, test_build_test_traceability_multiple_test_files, test_build_test_traceability_coverage_ratio_rounded, test_build_test_traceability_multiple_modules