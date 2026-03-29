---
codd:
  node_id: "req:extract-verify"
  type: requirements
  depends_on: []
---

# Extract & Verify — Requirements

## Background

CoDD's `extract` command reverse-engineers design documents from existing codebases (brownfield support). The `verify` command runs build+test verification and traces failures back to design documents. Both are core to the V-Model lifecycle.

## R1: Extract — Pluggable Multi-Category Extraction

The extractor must support 6 artifact categories through a pluggable architecture:

| Category | Artifacts | Priority |
|----------|-----------|----------|
| A: Source Code | Python, TypeScript, JavaScript (Tree-sitter); Go, Java (regex fallback) | HIGH |
| B: DDL/Schema | SQL DDL, Prisma, ORM definitions | HIGH |
| C: API Definitions | OpenAPI, GraphQL, Protobuf | MEDIUM |
| D: Config/Infra | docker-compose, Kubernetes, Terraform | LOW |
| E: Tests | pytest, Jest, Go test | MEDIUM |
| F: Build/Deps | pyproject.toml, package.json, go.mod | MEDIUM |

### R1.1: Pluggable Extractor Protocol
- `LanguageExtractor` protocol with `extract_symbols`, `extract_imports`, `detect_code_patterns`
- Factory function `get_extractor(language, category)` for runtime selection
- Graceful degradation: Tree-sitter → regex fallback on parse failure

### R1.2: V-Model Reverse Pipeline (3 stages)
- **Stage 1**: All artifacts (A-F) → `ProjectFacts` unified data model
- **Stage 2**: `ProjectFacts` → per-module detailed design Markdown (Jinja2 templates)
- **Stage 3**: Detailed design MDs → project-level architecture overview MD

### R1.3: CoDD Integration
- All generated documents must include CoDD YAML frontmatter (node_id, type, depends_on)
- Output directory respects `find_codd_dir()` discovery (`.codd/extracted/`)
- Generated docs are scannable by `codd scan`

## R2: Verify — Language-Agnostic Verification

The verifier must support multiple languages, not just TypeScript/Node.js.

### R2.1: Python Verification
- **Type check**: mypy or pyright (configurable)
- **Test runner**: pytest with JSON output (`--tb=short -q`)
- **Preflight**: Check for Python project indicators (pyproject.toml or setup.py or setup.cfg)
- **Error parsing**: Parse mypy/pyright and pytest output formats

### R2.2: Pluggable Verifier Architecture
- Language detection from `codd.yaml` `project.language` field
- Per-language preflight checks (no more hardcoded package.json requirement)
- Per-language typecheck and test commands with configurable defaults
- Design traceability: map failures to design documents via `@generated-from` comments

### R2.3: Configurable Defaults per Language

| Language | Typecheck | Test Runner | Preflight |
|----------|-----------|-------------|-----------|
| python | `mypy .` or `pyright` | `pytest --tb=short -q` | pyproject.toml or setup.py |
| typescript | `npx tsc --noEmit` | `npx jest --ci --json` | package.json, tsconfig.json |
| go | `go vet ./...` | `go test ./... -json` | go.mod |

### R2.4: Verification Report
- Markdown report output to `docs/test/verify_report.md`
- Includes: typecheck results, test results, design refs, propagation targets
- Works regardless of language

## R3: Dogfooding — CoDD Manages Itself

- codd-dev uses `.codd/` as config directory (since `codd/` is source code)
- All design documents for extract and verify are managed by CoDD
- `codd scan` builds the dependency graph including these docs
- `codd verify` can verify codd-dev itself (Python project)

## R4: Extract v2 — Beyond Import Dependencies

Current `codd extract` captures only import dependencies (structural edges). This is the "skeleton" — what depends on what. But three critical dimensions are missing to understand how a codebase actually works.

### R4.1: Call Graph Extraction

Import dependencies show "A knows about B." Call graphs show "A calls B.foo() at runtime."

- **Static call graph**: Tree-sitter AST analysis to extract function-to-function call edges
- **Output**: Per-module `call_graph` section listing caller → callee relationships
- **Scope**: Intra-project calls only (exclude stdlib/third-party)
- **Data flow direction**: Enables "request pipeline" reconstruction (e.g., routing → dependencies → endpoint → serialization → response)
- **Temporal ordering**: Call graph implies execution order, which import graph does not

### R4.2: Feature Clustering

Individual modules don't map 1:1 to user-visible features. Multiple modules collaborate to implement a feature.

- **Co-call analysis**: Modules frequently called together in the same call chain form a feature cluster
- **Heuristic signals**:
  - Shared prefixes in function/class names (e.g., `security_*`, `oauth2_*`)
  - Common callers (modules called by the same parent)
  - Cross-reference density (modules with bidirectional or high-frequency mutual calls)
- **Output**: `feature_clusters` section in architecture-overview.md listing inferred feature groups with member modules and confidence
- **Example**: `Authentication = {security, dependencies(DI injection), openapi(schema reflection), params(Form receipt)}`

### R4.3: Interface Contract Detection

Distinguish public API surface from internal implementation details.

- **Re-export analysis**: Symbols in `__init__.py` are public API; everything else is internal
- **Output per module**:
  - `public_api`: Symbols re-exported via `__init__.py` or explicitly in `__all__`
  - `internal_api`: Everything else
  - `api_surface_ratio`: public / total symbols
- **Cross-module contracts**: When module A only uses module B's public API vs reaching into internals
- **Encapsulation violations**: Internal symbols used by other modules = fragile coupling

### R4.4: Integration with Existing Extract Pipeline

- Call graph and feature clusters feed into Stage 3 (architecture overview) alongside import dependencies
- Interface contracts feed into per-module design docs (Stage 2)
- All new data included in CoDD frontmatter `depends_on` with semantic relation types:
  - `imports` (existing) — structural dependency
  - `calls` (new) — runtime invocation
  - `co_feature` (new) — feature cluster membership
- Confidence scoring: call graph edges have higher confidence than import-only edges

## R5: Extract v3 — Impact-Accurate Extraction for Safe Changes

Goal: Make `codd impact` output actionable for bug fixes and enhancements without regressions. Three gaps remain between "knowing what's affected" and "safely changing it."

### R5.1: Test Traceability (test → code → test)

Current state: Test files are mapped to modules by filename heuristic (`test_foo.py → foo`) or import analysis. This tells you "module foo has tests" but NOT "if you change `foo.authenticate()`, which specific tests cover it."

**Requirements:**
- **Test import analysis**: For each test file, extract which source modules and symbols it imports and calls
- **Test-to-symbol mapping**: Each test function maps to a set of source symbols it exercises (via call graph from test code)
- **Output per module**: `test_coverage` section listing:
  - `covered_symbols`: Source symbols that appear in at least one test's call graph
  - `uncovered_symbols`: Source symbols with zero test references
  - `coverage_ratio`: covered / total
- **Impact integration**: When `codd impact` marks a module as Amber, include `affected_tests` list — the specific test files/functions that should be re-run
- **Risk flag**: Modules with Amber impact + low coverage_ratio get `⚠ untested change` warning

### R5.2: Schema-Code Dependency

Current state: SQL/Prisma schemas are extracted (Category B) but stored separately from source modules. No link between "table `users`" and "code that queries `users`."

**Requirements:**
- **ORM model detection**: In source code, detect ORM model definitions that map to schema tables:
  - SQLAlchemy: `class User(Base)` with `__tablename__`
  - Django: `class User(models.Model)`
  - Prisma client: `prisma.user.find_many()`
  - Raw SQL: String literals containing `SELECT ... FROM users`, `INSERT INTO users`
- **Schema-to-module edges**: New dependency relation `schema_uses` linking source modules to schema artifacts
- **Output**: Per-module `schema_dependencies` section listing tables/models referenced
- **Impact integration**: When a schema document is changed, `codd impact` follows `schema_uses` edges to affected source modules

### R5.3: Runtime Wiring Detection

Current state: Import dependencies and call graph capture explicit code references. Framework-level implicit wiring is invisible.

**Requirements:**
- **Dependency injection**: Detect framework DI patterns:
  - FastAPI `Depends()` — the function passed to Depends is a runtime dependency
  - Django `MIDDLEWARE` list in settings.py
  - Flask `@app.before_request` / `@app.after_request`
- **Event/signal handlers**: Detect pub/sub patterns:
  - Django signals (`post_save.connect`)
  - Python `asyncio` event handlers
  - Custom event bus patterns (functions registered as handlers)
- **Decorator-based routing**: Link route decorators to middleware chains:
  - `@app.route` / `@router.get` → middleware pipeline
  - `@celery.task` → async task dependency
- **Output**: New dependency relation `runtime_wires` in frontmatter
- **Confidence**: Runtime wiring edges get `confidence: 0.6` (lower than call graph) because detection is heuristic

### R5.4: Change Risk Scoring

Current state: `codd impact` shows Green/Amber/Gray but doesn't quantify how risky a change is.

**Requirements:**
- **Per-module risk score** (0.0–1.0) computed from:
  - `dependents_count`: How many other modules depend on this one (import + call + runtime)
  - `test_coverage_ratio`: From R5.1 (low coverage = higher risk)
  - `api_surface_ratio`: From R4.3 (large public API = more breaking change surface)
  - `encapsulation_violations`: From R4.3 (internal symbols used externally = fragile)
- **Formula**: `risk = 0.3 * (dependents / max_dependents) + 0.3 * (1 - coverage_ratio) + 0.2 * api_surface_ratio + 0.2 * (violations / max_violations)`
- **Output in architecture-overview.md**: `## Change Risk Summary` table sorted by risk score
- **Impact integration**: `codd impact` output shows risk score next to each Amber module

## R6: Extract→Impact Bridge — End-to-End Brownfield Pipeline

Goal: Close the pipeline gap where `codd extract` generates design docs but `codd impact` cannot trace source file changes back to those docs. Without this bridge, the entire brownfield promise ("extract → impact → safe changes") is broken.

### Problem Statement

Current behavior after `codd extract` + `codd scan` + `codd impact --diff HEAD~1`:

1. Scanner creates `file:codd/extractor.py` nodes (from source scan, Phase 2)
2. Scanner creates `design:extract:extractor` nodes (from extracted doc frontmatter, Phase 1)
3. **No edge connects them.** The `file:` node has `path=codd/extractor.py`. The `design:` node has `path=.codd/extracted/modules/extractor.md`.
4. When `codd/extractor.py` changes, `_resolve_start_nodes()` finds `file:codd/extractor.py` and does `find_nodes_by_path("codd/extractor.py")` — but no `design:` node has that path.
5. Impact propagation starts from `file:codd/extractor.py` which has no dependents → "No impacts detected."

The extracted design docs (`design:extract:*`) DO have `depends_on` edges to each other (import/call/co_feature relations), forming a rich dependency sub-graph. But that sub-graph is unreachable from source file changes because no `file:` → `design:` edge exists.

### R6.1: Source File Mapping in Extracted Frontmatter

Extracted design docs must declare which source files they describe.

**Requirements:**
- `_build_frontmatter()` in synth.py gains a `source_files` parameter: `list[str]` of relative source file paths
- Per-module design docs include `source_files` in their CoDD YAML frontmatter:
  ```yaml
  codd:
    node_id: design:extract:extractor
    type: design
    source: extracted
    source_files:
      - codd/extractor.py
  ```
- The `source_files` list comes from `ModuleInfo.files` (already populated by Stage 1)
- Schema docs include their schema file path in `source_files`
- API docs include their spec file path in `source_files`
- Architecture overview and system-context do NOT have `source_files` (they are aggregations, not file-level)

### R6.2: Scanner Recognizes `source_files` and Creates Bridge Edges

The scanner must create `file:` → `design:` edges when it encounters `source_files` in frontmatter.

**Requirements:**
- In `_load_frontmatter()` (scanner.py Phase 1): when a document has `codd.source_files`, for each file path:
  1. Ensure the `file:{path}` node exists (create if Phase 2 hasn't run yet)
  2. Create an edge: `design:{node_id}` depends_on `file:{path}` with relation `extracted_from`
  3. Evidence: `{"origin": "auto:source_files", "detail": "extracted design doc maps to source file"}`
  4. Confidence: 0.85 (high — it's a direct 1:1 mapping from extraction)
- Edge direction: `design:extract:extractor` → `file:codd/extractor.py` (design depends on source)
- This means when `file:codd/extractor.py` changes, impact propagation follows the incoming edge to `design:extract:extractor`, then continues to all docs that depend on it

### R6.3: Impact Report Shows Design Doc Context

When `codd impact` detects that a source file change affects extracted design docs:

**Requirements:**
- Amber band items that are `design:extract:*` nodes should display:
  - The design doc's node_id and path
  - The module name (human-readable)
  - Change risk score (from R5.4, if available in the doc's frontmatter or graph metadata)
- The report should distinguish between:
  - **Direct**: Source file changed → its own design doc (depth 1)
  - **Transitive**: Source file changed → design doc → dependent design docs (depth 2+)

### R6.4: End-to-End Acceptance Test

The following sequence must produce meaningful impact output:

```bash
# 1. Extract design docs from source
codd extract --path .

# 2. Build dependency graph (with file→design bridge)
codd scan --path .

# 3. Change a source file
echo "# comment" >> codd/extractor.py

# 4. Impact analysis should find affected design docs
codd impact --diff HEAD --path .
# Expected: design:extract:extractor (Amber, depth 1)
#           design:extract:synth (Amber, depth 2, because synth imports extractor)
#           design:extract:architecture-overview (Amber, depth 2+)
```

## R7: TypeScript/JavaScript Call Graph Extraction

Current state: `extract_call_graph()` is implemented for Python (Tree-sitter AST traversal of `call` nodes) but returns `[]` for TypeScript and JavaScript. This means R4.1 (call graph), R4.2 (feature clustering), R5.1 (test traceability), and R5.4 (change risk) are all degraded for TS/JS projects — they fall back to import-only analysis.

### R7.1: TypeScript Call Graph via Tree-sitter

Extend `TreeSitterExtractor.extract_call_graph()` to handle TypeScript AST.

**Requirements:**
- Parse TypeScript source using tree-sitter-typescript
- Extract call expressions from AST nodes:
  | AST Node Type | Example | Caller | Callee |
  |---------------|---------|--------|--------|
  | `call_expression` | `foo()` | enclosing function/method | `foo` |
  | `call_expression` with `member_expression` | `this.bar()` | enclosing method | `ClassName.bar` |
  | `call_expression` with `member_expression` | `service.process()` | enclosing function | `service.process` |
  | `new_expression` | `new Foo()` | enclosing function | `Foo` (constructor) |
  | `await_expression` > `call_expression` | `await fetchData()` | enclosing function | `fetchData` (async) |
- Resolve callee names against known symbols from `extract_symbols()` output
- Exclude standard library and third-party calls (not in project symbol table)
- Handle TypeScript-specific patterns:
  - Optional chaining calls: `foo?.bar()` → callee is `foo.bar`
  - Type assertion calls: `(foo as Bar).baz()` → callee is `Bar.baz`
  - Generic calls: `foo<T>()` → callee is `foo`

### R7.2: JavaScript Call Graph (shared logic)

JavaScript call graph uses the same logic as TypeScript minus type annotations.

**Requirements:**
- Reuse TypeScript call graph implementation with JavaScript tree-sitter grammar
- Handle JavaScript-specific patterns:
  | Pattern | Example | Handling |
  |---------|---------|----------|
  | CommonJS require + call | `require('./foo').bar()` | callee is `foo.bar` |
  | Destructured import call | `const { bar } = require('./foo'); bar()` | callee is `foo.bar` |
  | Prototype method call | `Foo.prototype.bar.call(this)` | callee is `Foo.bar` |
- ES module imports (`import { bar } from './foo'`) already resolved by `extract_imports()`

### R7.3: Call Graph Quality Constraints

- **Precision over recall**: It's better to miss a call edge than to report a false one. Only emit edges where both caller and callee resolve to known project symbols.
- **Performance**: Call graph extraction must not add >50% to extraction time for a 1000-file TS project
- **Fallback**: If tree-sitter-typescript is not installed, return `[]` (same as current RegexExtractor behavior). Do not crash.

### R7.4: Integration with Downstream Features

Once TS/JS call graph is available, these features automatically improve:
- **R4.2 Feature Clustering**: TS/JS modules can form feature clusters based on call adjacency (currently import-only)
- **R5.1 Test Traceability**: Jest test files can map to source symbols via call graph (currently name-matching only)
- **R5.4 Change Risk**: `dependents_count` includes call-graph dependents (currently import-only for TS/JS)

No changes needed in clustering.py, traceability.py, or risk.py — they already consume `ModuleInfo.call_edges` generically.

## Acceptance Criteria

1. `codd extract` on any Python/TS project produces correct design docs with CoDD frontmatter
2. `codd verify` on codd-dev (Python) runs mypy + pytest and reports results
3. `codd scan` recognizes all generated design documents
4. All 200+ existing tests continue to pass
5. `codd extract` with call graph flag produces per-module call_graph sections (R4.1)
6. Architecture overview includes feature_clusters section when call graph data is available (R4.2)
7. Per-module docs include public_api / internal_api distinction (R4.3)
8. Per-module docs include test_coverage section with covered/uncovered symbols (R5.1)
9. Schema artifacts link to source modules via schema_uses relation (R5.2)
10. Runtime wiring (DI, middleware, signals) detected and included in dependency graph (R5.3)
11. Architecture overview includes Change Risk Summary table (R5.4)
12. Extracted design docs include `source_files` in frontmatter mapping to source file paths (R6.1)
13. `codd scan` creates `extracted_from` edges between `design:extract:*` and `file:*` nodes (R6.2)
14. `codd impact` on a source file change reports affected extracted design docs with depth and band (R6.3, R6.4)
15. TypeScript call graph extraction produces `CallEdge` entries for TS projects (R7.1)
16. JavaScript call graph extraction produces `CallEdge` entries for JS projects (R7.2)
