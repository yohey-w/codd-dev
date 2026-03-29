---
codd:
  node_id: "design:extract-architecture"
  type: design
  depends_on:
    - id: "req:extract-verify"
      relation: implements
---

# Extract Architecture Design

## Overview

`codd extract` reverse-engineers design documents from existing codebases through a 3-stage V-Model reverse pipeline. The architecture uses a pluggable extractor system supporting 6 artifact categories (A-F).

## Pluggable Extractor Protocol

```python
class LanguageExtractor(Protocol):
    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]
    def extract_imports(self, content: str, file_path: Path,
                        project_root: Path, src_dir: Path) -> tuple[dict, set]
    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None
```

### Extractor Hierarchy

```
LanguageExtractor (Protocol)
  ├── TreeSitterExtractor   # Category A: Source code (Python, TS, JS)
  ├── RegexExtractor        # Category A: Fallback (Java, Go)
  ├── SqlDdlExtractor       # Category B: DDL/Schema
  ├── ApiDefExtractor       # Category C: API definitions
  ├── ConfigExtractor       # Category D: Config/Infra
  ├── TestExtractor         # Category E: Test files
  └── BuildDepsExtractor    # Category F: Build/Dependencies
```

### Factory

```python
def get_extractor(language: str, category: str) -> LanguageExtractor:
    # Returns appropriate extractor with graceful degradation
    # Tree-sitter unavailable → RegexExtractor fallback
```

## V-Model Reverse Pipeline

### Stage 1: Artifacts → ProjectFacts

`extract_facts()` discovers and processes all 6 categories:

1. **Phase 1a**: Source code modules (Tree-sitter enhanced)
2. **Phase 1b**: Schema/DDL discovery (SQL, Prisma, ORM)
3. **Phase 1c**: API definitions (OpenAPI, GraphQL, Protobuf)
4. **Phase 1d**: Config/Infra (Docker, K8s, Terraform)
5. **Phase 1e**: Test mapping (pytest, Jest, Go test → source modules)
6. **Phase 1f**: Build/Dependencies (pyproject.toml, package.json, go.mod)

### Stage 2: ProjectFacts → Detailed Design MD

`synth_docs()` generates per-module Markdown using Jinja2 templates:

| Template | Output | Content |
|----------|--------|---------|
| module-detail.md.j2 | `modules/{slug}.md` | Symbols, imports, patterns, layer |
| schema-design.md.j2 | `schemas/{slug}.md` | Tables, FKs, indexes |
| api-contract.md.j2 | `api/{slug}.md` | Endpoints, schemas, services |
| system-context.md.j2 | `system-context.md` | Module map + dependency overview |

All generated docs include CoDD frontmatter with auto-derived `depends_on`.

### Stage 3: Detailed Design → Architecture Overview

`synth_architecture()` aggregates into a single `architecture-overview.md`:

1. System Overview (language, frameworks, ORM)
2. Architectural Layers (auto-classified: Presentation/Application/Domain/Infrastructure)
3. Module Dependency Graph (with layer violation detection)
4. Data Model Summary
5. API Surface
6. Infrastructure Topology
7. External Dependencies
8. Cross-Cutting Concerns
9. Entry Points & Deployment

## Config Directory Integration

Output directory resolves via `find_codd_dir()`:
- `.codd/` found → output to `.codd/extracted/`
- `codd/` found (with codd.yaml) → output to `codd/extracted/`
- Neither → output to `codd/extracted/` (legacy default)

## Files

- `codd/extractor.py` — Pipeline orchestration, ProjectFacts, extract_facts(), run_extract()
- `codd/parsing.py` — All extractor implementations (A-F)
- `codd/synth.py` — Jinja2 template engine, synth_docs(), synth_architecture()
- `codd/templates/extracted/` — Jinja2 templates
