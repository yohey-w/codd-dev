---
codd:
  node_id: design:extract:config
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
---
# config

> 1 files, 85 lines

**Layer Guess**: Infrastructure
**Responsibility**: Implements parsing, extraction, scanning, or adapters

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| function | `find_codd_dir` | `codd/config.py:19` | `find_codd_dir(project_root: Path) -> Path | None` |
| function | `load_project_config` | `codd/config.py:33` | `load_project_config(project_root: Path) -> dict[str, Any]` |







## External Dependencies

- `copy`
- `yaml`

## Files

- `codd/config.py`

## Tests

- `tests/test_config.py` — tests: test_load_project_config_merges_defaults_and_project_overrides