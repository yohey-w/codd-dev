---
codd:
  node_id: design:extract:contracts
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/contracts.py
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# contracts

> 1 files, 138 lines

**Layer Guess**: Infrastructure
**Responsibility**: Defaulted to infrastructure because no higher-level cues were detected

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `InterfaceContract` | `codd/contracts.py:20` | — |
| function | `detect_init_exports` | `codd/contracts.py:45` | `detect_init_exports(init_content: str) -> list[str]` |
| function | `build_interface_contracts` | `codd/contracts.py:79` | `build_interface_contracts(facts: ProjectFacts, project_root: Path) -> None` |






## Public API

- `InterfaceContract`
- `detect_init_exports`
- `build_interface_contracts`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `build_interface_contracts` | `detect_init_exports` | `codd/contracts.py:100` | no |
| `build_interface_contracts` | `InterfaceContract` | `codd/contracts.py:110` | no |

## Test Coverage

**Coverage**: 1.0 (3 / 3)
Tests: tests/test_contracts.py



## Import Dependencies

### → extractor

- `from codd.extractor import ProjectFacts`
- `from codd.extractor import _language_extensions`


## Files

- `codd/contracts.py`

## Tests

- `tests/test_contracts.py` — tests: test_detect_init_exports_all, test_detect_init_exports_reexports, test_detect_init_exports_all_takes_priority, test_detect_init_exports_empty, test_build_interface_contracts_with_init, test_build_interface_contracts_no_init, test_encapsulation_violations