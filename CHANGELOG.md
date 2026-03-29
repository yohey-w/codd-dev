# Changelog

All notable changes to CoDD are documented in this file.

## [0.2.0a5] - 2026-03-29

### Added

- **`codd extract` — Brownfield bootstrap from existing codebases**
  - Reverse-engineers CoDD design documents from source code using static analysis
  - No AI required — pure deterministic structural fact extraction
  - Philosophy: in V-Model, intent lives only in requirements; everything below
    is structural fact that static analysis can extract
  - Supports Python, TypeScript, JavaScript (full import + symbol extraction),
    Go (symbol + import extraction), Java (symbol extraction only; import tracing planned)
  - Two-phase architecture: extract-facts (static analysis) → synth-docs (templated Markdown)
  - Auto-detects language, source directories, test directories, frameworks, ORMs
  - Generates `system-context.md` (module map + dependency graph) and per-module
    design documents with full CoDD frontmatter
  - Module cards include: classes, public functions, internal/external dependencies,
    file list, test mapping, detected patterns (API routes, DB models)
  - Confidence scores capped below green band — human review always required
  - Works without `codd init` (true brownfield: no prior CoDD setup needed)
  - Output to `codd/extracted/` as draft documents; promote after review

## [0.2.0a1] - 2026-03-29

### Public Alpha Release

First public alpha of CoDD (Coherence-Driven Development). Core graph engine
and impact analysis are stable. Generation and verification are experimental.

### Added

- **V-Model verification phases** aligned with IPA Common Frame
  - Unit tests verify detailed design, integration tests verify system design,
    E2E tests verify requirements
  - Test strategy derived from architecture (no manual configuration)
- **Derivation principle**: upstream docs + best practices = downstream is self-evident
- `codd verify` command with V-Model loss function
- `codd implement` command for design-to-code generation
- `codd plan --init` for automatic wave config generation from requirements
- `codd generate` with AI-driven document content generation
- `codd validate` for frontmatter and dependency integrity checks
- `codd hooks install` for Git pre-commit integration
- Detailed design wave support (Wave 4.5)
- Prior task context injection to prevent code duplication in implementation

### Changed

- **Renamed CPDD to CoDD** (Coherence-Driven Development)
- Migrated graph store from SQLite to JSONL for portability
- Frontmatter is now the Single Source of Truth (graph data in codd/scan/ is a derived cache)
- README rewritten for competitive positioning against Spec Kit / OpenSpec

### Fixed

- Windows path normalization for cross-platform support
- Meta-commentary and AI artifact stripping in generated documents
- Wave config forward references no longer cause false errors (BLOCKED, not ERROR)
- Selective purge preserves human-authored evidence on scan refresh

### Core Commands (Stable)

| Command | Status |
|---------|--------|
| `codd init` | Stable |
| `codd scan` | Stable |
| `codd impact` | Stable |
| `codd validate` | Alpha |

### AI Commands (Experimental)

| Command | Status |
|---------|--------|
| `codd generate` | Experimental |
| `codd verify` | Experimental |
| `codd implement` | Experimental |
| `codd plan` | Experimental |

## [0.1.0] - 2026-02-15

### Initial Release (Internal)

- CEG (Conditioned Evidence Graph) with JSONL-backed dependency graph
- `codd init`, `codd scan`, `codd impact` CLI commands
- Frontmatter-first architecture
- Convention-aware impact propagation with Green/Amber/Gray bands
- Multi-agent operation guide (Shogun system integration)
