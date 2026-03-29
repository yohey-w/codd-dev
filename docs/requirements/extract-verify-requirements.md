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

## Acceptance Criteria

1. `codd extract` on any Python/TS project produces correct design docs with CoDD frontmatter
2. `codd verify` on codd-dev (Python) runs mypy + pytest and reports results
3. `codd scan` recognizes all generated design documents
4. All 127+ existing tests continue to pass
