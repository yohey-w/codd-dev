---
codd:
  node_id: design:extract:implementer
  type: design
  source: extracted
  confidence: 0.65
  last_extracted: '2026-03-30'
  depends_on:
  - id: design:extract:generator
    relation: imports
    semantic: technical
  - id: design:extract:scanner
    relation: imports
    semantic: technical
---
# implementer

> 1 files, 846 lines

**Layer Guess**: Application
**Responsibility**: Coordinates use cases or service-level workflows

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `ImplementationPlan` | `codd/implementer.py:47` | — |
| class | `ImplementationTask` | `codd/implementer.py:58` | — |
| class | `ImplementationResult` | `codd/implementer.py:75` | — |
| function | `implement_sprint` | `codd/implementer.py:85` | `implement_sprint(project_root: Path, sprint: int, *, task: str | None = None, ai_command: str | None = None,) -> list[ImplementationResult]` |






## Import Dependencies

### → generator

- `from codd.generator import DependencyDocument, _load_project_config, _normalize_conventions`
### → generator as generator_module

- `import codd.generator as generator_module`
### → scanner

- `from codd.scanner import _extract_frontmatter, build_document_node_path_map`


## Files

- `codd/implementer.py`

