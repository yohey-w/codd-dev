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

## Extract v3: Impact-Accurate Extraction (R5)

### Test Traceability (R5.1)

Extends `TestInfo` with symbol-level coverage data:

```python
@dataclass
class TestInfo:
    file_path: str
    test_functions: list[str]
    fixtures: list[str]
    source_module: str | None = None
    # R5.1 additions:
    tested_symbols: list[str] = field(default_factory=list)  # source symbols exercised
```

```python
@dataclass
class TestCoverage:
    module: str
    covered_symbols: list[str]
    uncovered_symbols: list[str]
    coverage_ratio: float         # covered / total
    covering_tests: list[str]     # test file paths
```

**Algorithm**: For each test file:
1. Extract call graph from test code (reuse R4.1 `extract_call_graph`)
2. Resolve callee names against source module symbols
3. Union of all resolved symbols = `tested_symbols`

**Pipeline integration**: Post-Stage 1. Stored in `ModuleInfo.test_coverage: TestCoverage | None`. Stage 2 template adds `## Test Coverage` section. Stage 3 adds uncovered-module warnings.

### Schema-Code Dependency (R5.2)

Detects ORM model and raw SQL references in source code:

```python
@dataclass
class SchemaRef:
    table_or_model: str      # "users" or "User"
    kind: str                # "sqlalchemy" | "django" | "prisma" | "raw_sql"
    file: str
    line: int
```

**Detection patterns**:
| Framework | Pattern | Example |
|-----------|---------|---------|
| SQLAlchemy | `__tablename__ = 'X'` in class body | `class User(Base): __tablename__ = 'users'` |
| Django | `class X(models.Model)` | `class User(models.Model)` |
| Prisma | `prisma.X.find_many/create/update/delete` | `prisma.user.find_many()` |
| Raw SQL | String literal containing SQL keywords + table name | `"SELECT * FROM users WHERE"` |

**Pipeline integration**: Collected in Stage 1 alongside symbols. Stored in `ModuleInfo.schema_refs: list[SchemaRef]`. Frontmatter gains `schema_uses` relation linking module → schema doc.

### Runtime Wiring Detection (R5.3)

Detects framework-specific implicit dependencies:

```python
@dataclass
class RuntimeWire:
    kind: str           # "depends" | "middleware" | "signal" | "decorator"
    source: str         # file:line
    target: str         # the function/class wired
    framework: str      # "fastapi" | "django" | "flask" | "celery"
```

**Detection patterns** (Tree-sitter + regex):
| Pattern | Framework | AST Query |
|---------|-----------|-----------|
| `Depends(fn)` | FastAPI | Call node with `Depends` function, extract arg |
| `MIDDLEWARE = [...]` | Django | Assignment to MIDDLEWARE variable |
| `@app.before_request` | Flask | Decorator with `before_request`/`after_request` |
| `signal.connect(fn)` | Django | Call to `.connect()` on signal objects |
| `@celery.task` | Celery | Decorator containing `task` |

**Pipeline integration**: Collected in Stage 1. Stored in `ModuleInfo.runtime_wires: list[RuntimeWire]`. Frontmatter gains `runtime_wires` relation. Confidence: 0.6 (heuristic).

### Change Risk Scoring (R5.4)

Post-extract analysis that scores each module's change risk:

```python
@dataclass
class ChangeRisk:
    module: str
    score: float           # 0.0–1.0
    factors: dict[str, float]  # breakdown per factor
```

**Formula**:
```
risk = 0.3 × (dependents / max_dependents)
     + 0.3 × (1 - coverage_ratio)
     + 0.2 × api_surface_ratio
     + 0.2 × (violations / max_violations)
```

Where:
- `dependents` = import + call + runtime_wire inbound edges
- `coverage_ratio` = from R5.1 TestCoverage
- `api_surface_ratio` = from R4.3 InterfaceContract
- `violations` = encapsulation violation count from R4.3

**Pipeline integration**: Computed post-Stage 1 (after R4 + R5.1-5.3). Stored in `ProjectFacts.change_risks: list[ChangeRisk]`. Stage 3 template adds `## Change Risk Summary` table.

### Updated Stage 3 Architecture Overview Sections

1. System Overview
2. Architectural Layers (existing)
3. Feature Clusters (R4.2)
4. Interface Contracts Summary (R4.3)
5. Layer Violations (existing)
6. **Change Risk Summary** (NEW — R5.4)
7. Module Dependency Graph (existing, enriched)
8. Data Model Summary
9. API Surface
10. External Dependencies
11. Cross-Cutting Concerns
12. Entry Points & Deployment

## Extract→Impact Bridge (R6)

### Problem

Extracted design docs (`design:extract:*`) form a rich dependency sub-graph, but `codd impact` cannot reach them from source file changes because no `file:` → `design:` edge exists.

### R6.1: Source File Mapping in Frontmatter

`_build_frontmatter()` gains `source_files: list[str]` parameter. Per-module docs include the module's source files in YAML frontmatter:

```yaml
codd:
  node_id: design:extract:extractor
  source_files:
    - codd/extractor.py
```

**Implementation**: `synth.py` passes `ModuleInfo.files` to `_build_frontmatter()`. Schema/API docs pass their artifact path.

### R6.2: Scanner Bridge Edges

In `_load_frontmatter()` (scanner.py Phase 1), when `codd.source_files` is present:

1. For each source file path, ensure `file:{path}` node exists
2. Create edge: `design:{node_id}` depends_on `file:{path}`
3. Relation: `extracted_from`, confidence: 0.85
4. Evidence: `{"origin": "auto:source_files"}`

Edge direction: `design:extract:extractor` → `file:codd/extractor.py` (design depends on source). When the file changes, impact follows the **incoming** edge to the design doc, then propagates to all dependents.

### R6.3: Impact Report Enhancement

Impact report shows design doc context for `design:extract:*` hits:
- Module name (human-readable)
- Depth (1 = direct, 2+ = transitive)
- Change risk score (if available)

## TypeScript/JavaScript Call Graph (R7)

### R7.1: TypeScript Call Graph via Tree-sitter

Extend `TreeSitterExtractor.extract_call_graph()` for TypeScript AST:

| AST Node | Example | Caller | Callee |
|----------|---------|--------|--------|
| `call_expression` | `foo()` | enclosing function | `foo` |
| `call_expression` + `member_expression` | `service.process()` | enclosing function | `service.process` |
| `new_expression` | `new Foo()` | enclosing function | `Foo` |
| `await_expression` > `call_expression` | `await fetchData()` | enclosing function | `fetchData` (async) |

**Resolution**: Callee names resolved against known symbols from `extract_symbols()`. Unresolved (stdlib/third-party) excluded.

**Optional chaining**: `foo?.bar()` → callee is `foo.bar`.

### R7.2: JavaScript Call Graph

Reuses TypeScript implementation with JS grammar. Additional patterns:
- `require('./foo').bar()` → callee `foo.bar`
- Prototype method calls → callee `Class.method`

### R7.3: Quality Constraints

- Precision > recall (no false edges)
- Tree-sitter unavailable → return `[]` (graceful degradation)
- No changes needed in clustering.py, traceability.py, risk.py (they consume `call_edges` generically)

## Files

- `codd/extractor.py` — Pipeline orchestration, ProjectFacts, extract_facts(), run_extract()
- `codd/parsing.py` — All extractor implementations (A-F), call graph extraction (R4.1, R7)
- `codd/synth.py` — Jinja2 template engine, synth_docs(), synth_architecture(), source_files mapping (R6.1)
- `codd/scanner.py` — Dependency graph builder, source_files bridge edges (R6.2)
- `codd/propagate.py` — Impact analysis, node resolution
- `codd/contracts.py` — Interface contract detection (R4.3)
- `codd/clustering.py` — Feature clustering (R4.2)
- `codd/traceability.py` — Test traceability (R5.1)
- `codd/schema_refs.py` — Schema-code dependency detection (R5.2)
- `codd/wiring.py` — Runtime wiring detection (R5.3)
- `codd/risk.py` — Change risk scoring (R5.4)
- `codd/templates/extracted/` — Jinja2 templates
