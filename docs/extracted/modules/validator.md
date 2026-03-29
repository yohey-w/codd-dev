---
codd:
  node_id: design:extract:validator
  type: design
  source: extracted
  confidence: 0.65
  last_extracted: '2026-03-30'
  source_files:
  - codd/validator.py
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






## Public API

- `ValidationIssue`
- `ValidationResult`
- `error_count`
- `blocked_count`
- `warning_count`
- `exit_code`
- `add`
- `status`
- `sorted_issues`
- `DocumentRecord`
- `run_validate`
- `validate_project`
- `FrontmatterParseResult`
- `dfs`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `ValidationResult.add` | `ValidationIssue` | `codd/validator.py:78` | no |
| `run_validate` | `validate_project` | `codd/validator.py:109` | no |
| `run_validate` | `status` | `codd/validator.py:111` | no |
| `run_validate` | `status` | `codd/validator.py:116` | no |
| `run_validate` | `sorted_issues` | `codd/validator.py:121` | no |
| `validate_project` | `ValidationResult` | `codd/validator.py:132` | no |
| `validate_project` | `add` | `codd/validator.py:143` | no |
| `validate_project` | `add` | `codd/validator.py:149` | no |
| `validate_project` | `add` | `codd/validator.py:163` | no |
| `validate_project` | `DocumentRecord` | `codd/validator.py:171` | no |
| `validate_project` | `add` | `codd/validator.py:192` | no |
| `validate_project` | `add` | `codd/validator.py:202` | no |
| `validate_project` | `add` | `codd/validator.py:205` | no |
| `validate_project` | `add` | `codd/validator.py:216` | no |
| `validate_project` | `add` | `codd/validator.py:226` | no |
| `validate_project` | `add` | `codd/validator.py:244` | no |
| `validate_project` | `add` | `codd/validator.py:255` | no |
| `_parse_codd_frontmatter` | `FrontmatterParseResult` | `codd/validator.py:281` | no |
| `_parse_codd_frontmatter` | `FrontmatterParseResult` | `codd/validator.py:290` | no |
| `_parse_codd_frontmatter` | `FrontmatterParseResult` | `codd/validator.py:300` | no |
| `_parse_codd_frontmatter` | `FrontmatterParseResult` | `codd/validator.py:308` | no |
| `_parse_codd_frontmatter` | `FrontmatterParseResult` | `codd/validator.py:315` | no |
| `_extract_service_boundary_modules` | `add` | `codd/validator.py:415` | no |
| `_build_adjacency` | `add` | `codd/validator.py:459` | no |
| `_build_adjacency` | `add` | `codd/validator.py:462` | no |
| `_find_cycles.dfs` | `add` | `codd/validator.py:479` | no |
| `_find_cycles.dfs` | `dfs` | `codd/validator.py:483` | no |
| `_find_cycles.dfs` | `add` | `codd/validator.py:487` | no |
| `_find_cycles` | `dfs` | `codd/validator.py:491` | no |

## Test Coverage

**Coverage**: 0.0 (0 / 14)

**Uncovered symbols**: `DocumentRecord`, `FrontmatterParseResult`, `ValidationIssue`, `ValidationResult`, `add`, `blocked_count`, `dfs`, `error_count`, `exit_code`, `run_validate`, `sorted_issues`, `status`, `validate_project`, `warning_count`



## External Dependencies

- `yaml`

## Files

- `codd/validator.py`

