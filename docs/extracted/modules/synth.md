---
codd:
  node_id: design:extract:synth
  type: design
  source: extracted
  confidence: 0.65
  last_extracted: '2026-03-30'
  source_files:
  - codd/synth.py
  depends_on:
  - id: design:extract:extractor
    relation: imports
    semantic: technical
---
# synth

> 1 files, 869 lines

**Layer Guess**: Infrastructure
**Responsibility**: Defaulted to infrastructure because no higher-level cues were detected

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| function | `synth_docs` | `codd/synth.py:56` | `synth_docs(facts: ProjectFacts, output_dir: Path) -> list[Path]` |
| function | `synth_architecture` | `codd/synth.py:110` | `synth_architecture(facts: ProjectFacts, output_dir: Path, *, env: Environment | None = None, today: str | None = None,) -> Path` |






## Public API

- `synth_docs`
- `synth_architecture`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `synth_docs` | `extractor.synth_architecture` | `codd/synth.py:104` | no |

## Test Coverage

**Coverage**: 0.0 (0 / 2)

**Uncovered symbols**: `synth_architecture`, `synth_docs`


## Import Dependencies

### → extractor

- `from codd.extractor import ModuleInfo, ProjectFacts, Symbol`

## External Dependencies

- `jinja2`
- `yaml`

## Files

- `codd/synth.py`

