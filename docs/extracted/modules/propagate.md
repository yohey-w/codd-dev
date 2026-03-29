---
codd:
  node_id: design:extract:propagate
  type: design
  source: extracted
  confidence: 0.7
  last_extracted: '2026-03-30'
  source_files:
  - codd/propagate.py
  depends_on:
  - id: design:extract:graph
    relation: imports
    semantic: technical
  - id: design:extract:scanner
    relation: imports
    semantic: technical
---
# propagate

> 1 files, 308 lines

**Layer Guess**: Infrastructure
**Responsibility**: Implements parsing, extraction, scanning, or adapters

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| function | `run_impact` | `codd/propagate.py:13` | `run_impact(project_root: Path, codd_dir: Path, diff_target: str, output_path: str = None)` |






## Public API

- `run_impact`


## Test Coverage

**Coverage**: 0.0 (0 / 1)
Tests: tests/test_propagate.py

**Uncovered symbols**: `run_impact`




## Import Dependencies

### → graph

- `from codd.graph import CEG`
### → scanner

- `from codd.scanner import _extract_frontmatter`

## External Dependencies

- `yaml`

## Files

- `codd/propagate.py`

## Tests

- `tests/test_propagate.py` — tests: test_resolve_start_nodes_from_frontmatter, test_resolve_start_nodes_from_file_node, test_resolve_start_nodes_unknown_file, test_resolve_start_nodes_no_duplicates, test_check_conventions_from_graph_direct, test_check_conventions_from_graph_via_parent, test_impact_propagation_from_document_node, test_find_nodes_by_path, test_get_convention_edges, test_full_impact_from_document_change; fixtures: ceg