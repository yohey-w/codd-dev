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

## Acceptance Criteria

1. `codd extract` on any Python/TS project produces correct design docs with CoDD frontmatter
2. `codd verify` on codd-dev (Python) runs mypy + pytest and reports results
3. `codd scan` recognizes all generated design documents
4. All 127+ existing tests continue to pass
5. `codd extract` with call graph flag produces per-module call_graph sections (R4.1)
6. Architecture overview includes feature_clusters section when call graph data is available (R4.2)
7. Per-module docs include public_api / internal_api distinction (R4.3)
