---
codd:
  node_id: design:extract:wiring
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/wiring.py
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# wiring

> 1 files, 146 lines

**Layer Guess**: Infrastructure
**Responsibility**: Defaulted to infrastructure because no higher-level cues were detected

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `RuntimeWire` | `codd/wiring.py:20` | — |
| function | `detect_runtime_wires` | `codd/wiring.py:62` | `detect_runtime_wires(content: str, file_path: str) -> list[RuntimeWire]` |
| function | `build_runtime_wires` | `codd/wiring.py:134` | `build_runtime_wires(facts: ProjectFacts, project_root: Path) -> None` |






## Public API

- `RuntimeWire`
- `detect_runtime_wires`
- `build_runtime_wires`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `detect_runtime_wires` | `RuntimeWire` | `codd/wiring.py:72` | no |
| `detect_runtime_wires` | `RuntimeWire` | `codd/wiring.py:84` | no |
| `detect_runtime_wires` | `RuntimeWire` | `codd/wiring.py:94` | no |
| `detect_runtime_wires` | `RuntimeWire` | `codd/wiring.py:103` | no |
| `detect_runtime_wires` | `RuntimeWire` | `codd/wiring.py:113` | no |
| `detect_runtime_wires` | `RuntimeWire` | `codd/wiring.py:124` | no |
| `build_runtime_wires` | `detect_runtime_wires` | `codd/wiring.py:144` | no |

## Test Coverage

**Coverage**: 1.0 (3 / 3)
Tests: tests/test_wiring.py



## Import Dependencies

### → extractor

- `from codd.extractor import ProjectFacts`


## Files

- `codd/wiring.py`

## Tests

- `tests/test_wiring.py` — tests: test_runtime_wire_fields, test_detect_fastapi_depends_simple, test_detect_fastapi_depends_multiple_on_same_line, test_detect_fastapi_depends_dotted_target, test_detect_django_post_save_signal, test_detect_django_pre_delete_signal, test_detect_django_non_signal_connect_not_captured, test_detect_django_middleware, test_detect_flask_before_request, test_detect_flask_after_request, test_detect_flask_teardown_appcontext, test_detect_celery_task_decorator, test_detect_celery_shared_task, test_detect_generic_on_event, test_detect_no_wires_plain_code, test_detect_source_includes_line_number, test_build_runtime_wires_populates_module, test_build_runtime_wires_missing_file_skipped, test_build_runtime_wires_multiple_files