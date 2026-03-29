---
codd:
  node_id: design:extract:scanner
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/scanner.py
  depends_on:
  - id: design:extract:generator
    relation: imports
    semantic: technical
  - id: design:extract:graph
    relation: imports
    semantic: technical
  - id: design:extract:parsing
    relation: imports
    semantic: technical
---
# scanner

> 1 files, 465 lines

**Layer Guess**: Infrastructure
**Responsibility**: Implements parsing, extraction, scanning, or adapters

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| function | `run_scan` | `codd/scanner.py:20` | `run_scan(project_root: Path, codd_dir: Path)` |
| function | `build_document_node_path_map` | `codd/scanner.py:139` | `build_document_node_path_map(project_root: Path, config: dict[str, Any]) -> dict[str, Path]` |






## Public API

- `run_scan`
- `build_document_node_path_map`


## Test Coverage

**Coverage**: 0.5 (1 / 2)
Tests: tests/test_scanner.py

**Uncovered symbols**: `build_document_node_path_map`


## Import Dependencies

### → generator

- `from codd.generator import _load_wave_artifacts`
- `from codd.generator import _load_wave_artifacts`
### → graph

- `from codd.graph import CEG`
### → parsing

- `from codd.parsing import get_extractor`

## External Dependencies

- `fnmatch`
- `yaml`

## Files

- `codd/scanner.py`

## Tests

- `tests/test_scanner.py` — tests: test_extract_frontmatter_with_codd, test_extract_frontmatter_without_codd, test_extract_frontmatter_no_frontmatter, test_load_frontmatter_depends_on, test_load_frontmatter_conventions, test_load_frontmatter_data_dependencies, test_scan_refreshes_auto_generated_not_accumulate, test_scan_preserves_human_evidence, test_scan_warns_when_docs_markdown_is_missing_frontmatter, test_scan_warns_when_design_document_has_no_dependencies, test_scan_warns_when_wave_config_output_is_missing; fixtures: ceg