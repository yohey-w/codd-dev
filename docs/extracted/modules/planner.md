---
codd:
  node_id: design:extract:planner
  type: design
  source: extracted
  confidence: 0.65
  last_extracted: '2026-03-30'
  depends_on:
  - id: design:extract:config
    relation: imports
    semantic: technical
  - id: design:extract:generator
    relation: imports
    semantic: technical
  - id: design:extract:validator
    relation: imports
    semantic: technical
---
# planner

> 1 files, 580 lines

**Layer Guess**: Application
**Responsibility**: Coordinates use cases or service-level workflows

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `PlannedArtifact` | `codd/planner.py:52` | — |
| class | `PlannedWave` | `codd/planner.py:65` | — |
| class | `PlanResult` | `codd/planner.py:74` | — |
| class | `RequirementDocument` | `codd/planner.py:84` | — |
| class | `PlanInitResult` | `codd/planner.py:93` | — |
| class | `_ExternalNode` | `codd/planner.py:103` | — |
| function | `plan_init` | `codd/planner.py:108` | `plan_init(project_root: Path, *, force: bool = False, ai_command: str | None = None,) -> PlanInitResult` |
| function | `build_plan` | `codd/planner.py:147` | `build_plan(project_root: Path) -> PlanResult` |
| function | `render_plan_text` | `codd/planner.py:215` | `render_plan_text(plan: PlanResult) -> str` |
| function | `plan_to_dict` | `codd/planner.py:252` | `plan_to_dict(plan: PlanResult) -> dict` |






## Import Dependencies

### → config

- `from codd.config import find_codd_dir`
- `from codd.config import find_codd_dir`
### → generator

- `from codd.generator import WaveArtifact, _load_project_config, _load_wave_artifacts`
### → generator as generator_module

- `import codd.generator as generator_module`
### → validator

- `from codd.validator import _iter_doc_files, _parse_codd_frontmatter, validate_project`

## External Dependencies

- `yaml`

## Files

- `codd/planner.py`

