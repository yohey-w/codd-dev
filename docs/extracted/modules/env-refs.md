---
codd:
  node_id: design:extract:env-refs
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/env_refs.py
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# env_refs

> 1 files, 204 lines

**Layer Guess**: Infrastructure
**Responsibility**: Defaulted to infrastructure because no higher-level cues were detected

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `EnvRef` | `codd/env_refs.py:19` | — |
| function | `detect_env_refs` | `codd/env_refs.py:102` | `detect_env_refs(content: str, file_path: str) -> list[EnvRef]` |
| function | `build_env_refs` | `codd/env_refs.py:192` | `build_env_refs(facts: "ProjectFacts", project_root: Path) -> None` |






## Public API

- `EnvRef`
- `detect_env_refs`
- `build_env_refs`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `detect_env_refs` | `EnvRef` | `codd/env_refs.py:111` | no |
| `detect_env_refs` | `EnvRef` | `codd/env_refs.py:121` | no |
| `detect_env_refs` | `EnvRef` | `codd/env_refs.py:131` | no |
| `detect_env_refs` | `EnvRef` | `codd/env_refs.py:141` | no |
| `detect_env_refs` | `EnvRef` | `codd/env_refs.py:151` | no |
| `detect_env_refs` | `EnvRef` | `codd/env_refs.py:161` | no |
| `detect_env_refs` | `EnvRef` | `codd/env_refs.py:171` | no |
| `detect_env_refs` | `EnvRef` | `codd/env_refs.py:181` | no |
| `build_env_refs` | `detect_env_refs` | `codd/env_refs.py:202` | no |

## Test Coverage

**Coverage**: 1.0 (3 / 3)
Tests: tests/test_env_refs.py



## Environment Dependencies

| Key | Kind | Location | Default |
|-----|------|----------|---------|
| `KEY` | env | `codd/env_refs.py:30` | no |
| `KEY` | env | `codd/env_refs.py:30` | yes |
| `KEY` | env | `codd/env_refs.py:35` | no |
| `KEY` | env | `codd/env_refs.py:40` | no |
| `KEY` | env | `codd/env_refs.py:40` | yes |
| `KEY` | env | `codd/env_refs.py:45` | no |
| `KEY` | env | `codd/env_refs.py:45` | yes |
| `KEY` | env | `codd/env_refs.py:52` | no |
| `KEY` | env | `codd/env_refs.py:57` | no |
| `KEY` | env | `codd/env_refs.py:57` | no |
| `KEY` | config | `codd/env_refs.py:64` | no |
| `KEY` | config | `codd/env_refs.py:64` | no |
| `KEY` | config | `codd/env_refs.py:64` | no |
| `UPPER_CASE_ATTR` | config | `codd/env_refs.py:69` | no |
| `KEY` | env | `codd/env_refs.py:83` | yes |
| `KEY` | env | `codd/env_refs.py:84` | no |
| `KEY` | env | `codd/env_refs.py:119` | no |
| `KEY` | env | `codd/env_refs.py:149` | no |
| `KEY` | env | `codd/env_refs.py:159` | no |
| `UPPER_CASE` | config | `codd/env_refs.py:179` | no |


## Import Dependencies

### → extractor

- `from codd.extractor import ProjectFacts`


## Files

- `codd/env_refs.py`

## Tests

- `tests/test_env_refs.py` — tests: test_os_getenv_no_default, test_os_getenv_with_default, test_os_environ_bracket, test_os_environ_get, test_os_environ_get_no_default, test_os_environ_pop, test_multiple_env_refs_same_line, test_line_number_tracking, test_process_env_dot, test_process_env_bracket, test_process_env_dot_ignores_lowercase, test_process_env_bracket_allows_any_case, test_config_bracket, test_settings_attr, test_app_config_bracket, test_current_app_config, test_plain_string, test_comment_line, test_empty_content, test_populates_module_env_refs, test_handles_missing_file