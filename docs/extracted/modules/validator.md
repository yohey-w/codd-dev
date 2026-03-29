---
codd:
  node_id: design:extract:validator
  type: design
  source: extracted
  confidence: 0.65
  last_extracted: '2026-03-30'
---
# validator

> 1 files, 500 lines

**Layer Guess**: Infrastructure
**Responsibility**: Implements parsing, extraction, scanning, or adapters

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `ValidationIssue` | `codd/validator.py:49` | — |
| class | `ValidationResult` | `codd/validator.py:57` | — |
| class | `DocumentRecord` | `codd/validator.py:98` | — |
| class | `FrontmatterParseResult` | `codd/validator.py:261` | — |
| function | `error_count` | `codd/validator.py:62` | `error_count(self) -> int` |
| function | `blocked_count` | `codd/validator.py:66` | `blocked_count(self) -> int` |
| function | `warning_count` | `codd/validator.py:70` | `warning_count(self) -> int` |
| function | `exit_code` | `codd/validator.py:74` | `exit_code(self) -> int` |
| function | `add` | `codd/validator.py:77` | `add(self, level: str, code: str, location: str, message: str)` |
| function | `status` | `codd/validator.py:80` | `status(self) -> str` |
| function | `sorted_issues` | `codd/validator.py:89` | `sorted_issues(self) -> list[ValidationIssue]` |
| function | `run_validate` | `codd/validator.py:107` | `run_validate(project_root: Path, codd_dir: Path) -> int` |
| function | `validate_project` | `codd/validator.py:126` | `validate_project(project_root: Path, codd_dir: Path | None = None) -> ValidationResult` |
| function | `dfs` | `codd/validator.py:472` | `dfs(node: str)` |







## External Dependencies

- `yaml`

## Files

- `codd/validator.py`

