---
codd:
  node_id: design:extract:inheritance
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/inheritance.py
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# inheritance

> 1 files, 223 lines

**Layer Guess**: Infrastructure
**Responsibility**: Defaulted to infrastructure because no higher-level cues were detected

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `InheritanceEdge` | `codd/inheritance.py:22` | — |
| function | `build_inheritance_tree` | `codd/inheritance.py:56` | `build_inheritance_tree(facts: ProjectFacts) -> None` |
| function | `get_overrides` | `codd/inheritance.py:136` | `get_overrides(facts: ProjectFacts) -> dict[str, list[str]]` |
| function | `get_inherited_methods` | `codd/inheritance.py:161` | `get_inherited_methods(facts: ProjectFacts) -> dict[str, list[str]]` |






## Public API

- `InheritanceEdge`
- `build_inheritance_tree`
- `get_overrides`
- `get_inherited_methods`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `build_inheritance_tree` | `InheritanceEdge` | `codd/inheritance.py:124` | no |

## Test Coverage

**Coverage**: 1.0 (4 / 4)
Tests: tests/test_inheritance.py





## Import Dependencies

### → extractor

- `from codd.extractor import ProjectFacts`


## Files

- `codd/inheritance.py`

## Tests

- `tests/test_inheritance.py` — tests: test_resolves_parent_in_same_module, test_resolves_parent_across_modules, test_skips_builtin_bases, test_skips_unresolved_bases, test_no_self_loop, test_multiple_parents, test_ignores_non_class_symbols, test_qualified_base_resolution, test_detects_overridden_method, test_no_overrides, test_detects_inherited_methods, test_no_inherited_when_all_overridden, test_module_depends_on_includes_inherits