---
codd:
  node_id: design:extract:risk
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/risk.py
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# risk

> 1 files, 100 lines

**Layer Guess**: Infrastructure
**Responsibility**: Defaulted to infrastructure because no higher-level cues were detected

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `ChangeRisk` | `codd/risk.py:20` | — |
| function | `build_change_risks` | `codd/risk.py:27` | `build_change_risks(facts: ProjectFacts) -> None` |






## Public API

- `ChangeRisk`
- `build_change_risks`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `build_change_risks` | `ChangeRisk` | `codd/risk.py:89` | no |

## Test Coverage

**Coverage**: 1.0 (2 / 2)
Tests: tests/test_risk.py





## Import Dependencies

### → extractor

- `from codd.extractor import ProjectFacts`


## Files

- `codd/risk.py`

## Tests

- `tests/test_risk.py` — tests: test_change_risk_defaults, test_build_change_risks_empty_facts, test_build_change_risks_single_module_no_deps, test_build_change_risks_formula, test_build_change_risks_fully_covered_reduces_score, test_build_change_risks_sorted_descending, test_build_change_risks_factors_dict_keys, test_build_change_risks_no_interface_contract, test_build_change_risks_no_test_coverage, test_build_change_risks_call_edge_increments_dependent, test_build_change_risks_runtime_wire_increments_dependent, test_build_change_risks_all_zero_values