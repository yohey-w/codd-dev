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

## Extract v2: Beyond Import Dependencies (R4)

### Call Graph Extraction (R4.1)

Extends `LanguageExtractor` protocol with a new method:

```python
class LanguageExtractor(Protocol):
    # ... existing methods ...
    def extract_call_graph(self, content: str, file_path: str,
                           symbols: list[Symbol]) -> list[CallEdge]
```

```python
@dataclass
class CallEdge:
    caller: str          # "module.Class.method" or "module.function"
    callee: str          # target symbol (resolved to module if possible)
    call_site: str       # file:line
    is_async: bool       # async call
```

**Implementation**: Tree-sitter query for `call` nodes in AST. Resolve callee name against known symbols from Stage 1 `extract_symbols()` output. Unresolved calls (stdlib, third-party) are excluded.

**Pipeline integration**: Call edges collected in Stage 1 alongside imports. Stored in `ModuleInfo.call_edges: list[CallEdge]`.

### Feature Clustering (R4.2)

Post-Stage 1 analysis that groups modules by functional cohesion:

```python
@dataclass
class FeatureCluster:
    name: str                    # inferred feature name
    modules: list[str]           # member module names
    confidence: float            # 0.0-1.0
    evidence: list[str]          # why these are grouped
```

**Algorithm**:
1. Build call chain graph from CallEdges
2. Identify strongly-connected components (modules that mutually call each other)
3. Augment with heuristic signals:
   - Shared naming prefixes (`security_*`, `oauth2_*`)
   - Common caller analysis (modules called by the same parent)
   - Cross-reference density
4. Merge components with high affinity into clusters

**Output**: `feature_clusters` section in `architecture-overview.md`.

### Interface Contract Detection (R4.3)

Analyzes `__init__.py` re-exports and `__all__` to distinguish public/internal API:

```python
@dataclass
class InterfaceContract:
    module: str
    public_symbols: list[str]     # in __init__.py or __all__
    internal_symbols: list[str]   # everything else
    api_surface_ratio: float      # public / total
    encapsulation_violations: list[str]  # internals used by other modules
```

**Pipeline integration**: Stage 2 per-module docs include `## Public API` / `## Internal API` sections. Stage 3 architecture overview includes encapsulation violation summary.

### Updated Dependency Relation Types

Frontmatter `depends_on` entries gain semantic relation types:

| Relation | Source | Meaning |
|----------|--------|---------|
| `imports` | Stage 1 (existing) | A imports symbols from B |
| `calls` | R4.1 call graph | A invokes functions in B at runtime |
| `co_feature` | R4.2 clustering | A and B collaborate on the same feature |

### Updated Stage 3 Architecture Overview Sections

1. System Overview
2. Architectural Layers (existing)
3. **Request Pipeline** (NEW — derived from call graph)
4. **Feature Clusters** (NEW — R4.2 output)
5. Module Dependency Graph (existing, enriched with call edges)
6. **Interface Contracts Summary** (NEW — R4.3 public vs internal)
7. Layer Violations (existing, enriched with encapsulation violations)
8. Data Model Summary
9. API Surface
10. External Dependencies
11. Cross-Cutting Concerns
12. Entry Points & Deployment

## Files

- `codd/extractor.py` — Pipeline orchestration, ProjectFacts, extract_facts(), run_extract()
- `codd/parsing.py` — All extractor implementations (A-F), call graph extraction (R4.1)
- `codd/synth.py` — Jinja2 template engine, synth_docs(), synth_architecture(), feature clustering (R4.2)
- `codd/contracts.py` — Interface contract detection (R4.3) [NEW]
- `codd/templates/extracted/` — Jinja2 templates (updated for R4 sections)
