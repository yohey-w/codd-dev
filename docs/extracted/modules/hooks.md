---
codd:
  node_id: design:extract:hooks
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/hooks.py
  depends_on:
  - id: design:extract:config
    relation: imports
    semantic: technical
  - id: design:extract:scanner
    relation: imports
    semantic: technical
  - id: design:extract:validator
    relation: imports
    semantic: technical
---
# hooks

> 1 files, 110 lines

**Layer Guess**: Infrastructure
**Responsibility**: Implements parsing, extraction, scanning, or adapters

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| function | `install_pre_commit_hook` | `codd/hooks.py:18` | `install_pre_commit_hook(project_root: Path) -> tuple[Path, bool]` |
| function | `run_pre_commit` | `codd/hooks.py:48` | `run_pre_commit(project_root: Path) -> int` |






## Public API

- `install_pre_commit_hook`
- `run_pre_commit`


## Test Coverage

**Coverage**: 0.0 (0 / 2)
Tests: tests/test_hooks.py

**Uncovered symbols**: `install_pre_commit_hook`, `run_pre_commit`


## Import Dependencies

### → config

- `from codd.config import find_codd_dir`
### → scanner

- `from codd.scanner import _extract_frontmatter`
### → validator

- `from codd.validator import run_validate`

## External Dependencies

- `yaml`

## Files

- `codd/hooks.py`

## Tests

- `tests/test_hooks.py` — tests: test_pre_commit_blocks_staged_markdown_without_frontmatter, test_pre_commit_allows_valid_staged_markdown, test_hooks_install_creates_pre_commit_symlink