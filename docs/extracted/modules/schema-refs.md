---
codd:
  node_id: design:extract:schema-refs
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/schema_refs.py
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# schema_refs

> 1 files, 122 lines

**Layer Guess**: Domain
**Responsibility**: Owns schema, persistence, or domain model concepts

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `SchemaRef` | `codd/schema_refs.py:19` | — |
| function | `detect_schema_refs` | `codd/schema_refs.py:51` | `detect_schema_refs(content: str, file_path: str) -> list[SchemaRef]` |
| function | `build_schema_refs` | `codd/schema_refs.py:110` | `build_schema_refs(facts: ProjectFacts, project_root: Path) -> None` |






## Public API

- `SchemaRef`
- `detect_schema_refs`
- `build_schema_refs`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `detect_schema_refs` | `SchemaRef` | `codd/schema_refs.py:60` | no |
| `detect_schema_refs` | `SchemaRef` | `codd/schema_refs.py:70` | no |
| `detect_schema_refs` | `SchemaRef` | `codd/schema_refs.py:80` | no |
| `detect_schema_refs` | `SchemaRef` | `codd/schema_refs.py:100` | no |
| `build_schema_refs` | `detect_schema_refs` | `codd/schema_refs.py:120` | no |

## Test Coverage

**Coverage**: 1.0 (3 / 3)
Tests: tests/test_schema_refs.py

## Schema Dependencies

| Table/Model | Kind | Location |
|-------------|------|----------|
| `users` | sqlalchemy | `codd/schema_refs.py:29` |
| `User` | django | `codd/schema_refs.py:34` |
| `user` | prisma | `codd/schema_refs.py:39` |




## Import Dependencies

### → extractor

- `from codd.extractor import ProjectFacts`


## Files

- `codd/schema_refs.py`

## Tests

- `tests/test_schema_refs.py` — tests: test_detect_sqlalchemy_tablename_single_quotes, test_detect_sqlalchemy_tablename_double_quotes, test_detect_sqlalchemy_tablename_with_spaces, test_detect_django_model_standard, test_detect_django_model_abstract_user, test_detect_django_model_abstract_base_user, test_detect_django_model_line_number, test_detect_prisma_find_many, test_detect_prisma_create, test_detect_prisma_multiple_ops, test_detect_raw_sql_select, test_detect_raw_sql_insert_into, test_detect_raw_sql_keywords_not_captured_as_tables, test_detect_no_refs_plain_code, test_build_schema_refs_populates_module, test_build_schema_refs_missing_file_skipped, test_build_schema_refs_multiple_modules