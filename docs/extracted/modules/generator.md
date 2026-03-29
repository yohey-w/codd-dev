---
codd:
  node_id: design:extract:generator
  type: design
  source: extracted
  confidence: 0.65
  last_extracted: '2026-03-30'
  source_files:
  - codd/generator.py
  depends_on:
  - id: design:extract:config
    relation: imports
    semantic: technical
  - id: design:extract:scanner
    relation: imports
    semantic: technical
---
# generator

> 1 files, 648 lines

**Layer Guess**: Application
**Responsibility**: Coordinates use cases or service-level workflows

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `WaveArtifact` | `codd/generator.py:76` | — |
| class | `GenerationResult` | `codd/generator.py:88` | — |
| class | `DependencyDocument` | `codd/generator.py:97` | — |
| function | `generate_wave` | `codd/generator.py:105` | `generate_wave(project_root: Path, wave: int, force: bool = False, ai_command: str | None = None,) -> list[GenerationResult]` |






## Public API

- `WaveArtifact`
- `GenerationResult`
- `DependencyDocument`
- `generate_wave`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `generate_wave` | `GenerationResult` | `codd/generator.py:129` | no |
| `generate_wave` | `GenerationResult` | `codd/generator.py:147` | no |
| `_load_wave_artifacts` | `WaveArtifact` | `codd/generator.py:186` | no |
| `_load_dependency_documents` | `DependencyDocument` | `codd/generator.py:339` | no |

## Test Coverage

**Coverage**: 0.0 (0 / 4)

**Uncovered symbols**: `DependencyDocument`, `GenerationResult`, `WaveArtifact`, `generate_wave`


## Import Dependencies

### → config

- `from codd.config import load_project_config`
### → scanner

- `from codd.scanner import build_document_node_path_map`

## External Dependencies

- `copy`
- `shlex`
- `yaml`

## Files

- `codd/generator.py`

