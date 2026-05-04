# Changelog

All notable changes to CoDD are documented in this file.

## [1.11.0] - 2026-05-04

### Added

- **`FileSystemRouteExtractor`** — convention-driven extractor for filesystem-routing frameworks (Next.js App Router/Pages, SvelteKit, Nuxt 3, Astro, Remix). Declared in `codd.yaml` `filesystem_routes:` block. Generates endpoint nodes from directory structure with dynamic segment / group / parallel route normalization.
- **`DocumentUrlLinker`** — scans design/requirement node text for URL strings via regex and auto-creates edges to matching endpoint nodes. Format-agnostic (Mermaid, ASCII art, prose). Configured via `codd.yaml` `document_url_linking:` block.
- **`codd drift` command** — set-difference between design-referenced URLs and implementation endpoints. Reports design-only / impl-only drifts with closest-match suggestions. Exit code 1 on drift for CI integration.
- **`codd extract --layer routes`** — reverse-extracts filesystem routes into Mermaid screen-flow diagrams. Role-based subgraph splitting via URL-prefix inference. Useful for brownfield projects to bootstrap design docs from existing implementations.
- **Filesystem Routing Adapter Recipes** — README section with 5 ready-to-use codd.yaml examples for Next.js / Remix / SvelteKit / Nuxt 3 / Astro.

### Notes

- Validated on osato-lms (large Next.js codebase): endpoint nodes 1 → 109, drift detection 186 cases. See cmd_334 report.
- Generality Gate enforced: no framework-specific hardcoding in `routes_extractor.py` or `drift.py` core. All conventions declared via `codd.yaml`.

## [1.10.0] - 2026-04-19

### Added

- **Multi-language support in `codd implement`** — `implementer.py` now respects `project.language` from `codd.yaml` instead of hardcoding TypeScript. Added `LANGUAGE_EXT_MAP` with 12 languages (TypeScript, JavaScript, Python, Rust, Go, Java, Kotlin, Swift, C++, C, C#, Ruby). `LANGUAGE_ALIASES` normalizes shorthand variants (`ts`, `py`, `rs`, `golang`, etc.). Language-specific comment prefixes (`// @generated-by:` for C-family, `# @generated-by:` for Python/Ruby) and code fence markers also applied automatically. TypeScript remains the default fallback for backward compatibility. Resolves [#12](https://github.com/yohey-w/codd-dev/issues/12).

### Fixed

- **`codd implement` excludes failed task summaries from downstream prompts** — failed task output is no longer injected into subsequent phase prompts, preventing error noise from propagating through the implementation pipeline.

## [1.9.0] - 2026-04-16

### Added

- **Multi-AI engine support for `codd implement`** — file-writing agents (Codex) detected automatically via `_is_file_writing_agent()`. Git-based change capture: baseline → run agent → `git diff` → format as `=== FILE: ===` blocks → revert. Claude interactive mode (without `-p`) also supported as file-writing agent.
- **Automatic parallel execution within phases** — tasks grouped by phase number (`m1.x`, `m2.x`). Same-phase tasks run concurrently via `ThreadPoolExecutor` (max 4 workers). File-writing agents use git worktree isolation to prevent conflicts. Stdout agents parallelize without overhead.
- **Phase milestone parser** — `#### M1.1 Title（period）` format extracted from `## Milestones` section. Takes priority over Sprint and legacy Milestone formats.
- **`_group_tasks_by_phase()`** — groups `ImplementationTask` list by phase number for parallel scheduling.
- **`_execute_task()`** — extracted single-task execution into reusable function.
- **`_execute_phase_parallel()`** — orchestrates concurrent execution with worktree isolation for file-writing agents.
- **`_create_worktree()` / `_remove_worktree()`** — git worktree lifecycle management for parallel Codex execution.

### Changed

- AI command timeout increased from 600s to **3600s** (1 hour) for heavy reasoning models (e.g., GPT-5.4 xhigh).
- `implement_tasks()` now processes phases sequentially with intra-phase parallelism by default. No flag needed.
- `_invoke_ai_command()` accepts `project_root` kwarg to route file-writing agents.

## [1.8.0] - 2026-04-14

### Added

- **Diagnostic reasoning step in `codd fix`** — AI must now produce a `## Diagnosis` section identifying the root cause *before* writing any code fix. Prevents blind trial-and-error patching.
- **Session state persistence across retries** — `_SessionState` accumulates prior attempt history (diagnosis, approach, outcome) and injects it into subsequent retry prompts as `## Prior attempts (DO NOT repeat these)`. Eliminates repeated failed approaches.
- **`diagnosis` field on `FixAttempt`** — extracted root cause diagnosis is stored per attempt for downstream analysis and reporting.

### Changed

- `_build_fix_prompt()` now enforces a two-step workflow: Step 1 (Diagnose) → Step 2 (Fix). Retry prompts include full session history.
- `run_fix()` loop creates `_SessionState` and records each failed attempt before retrying.

### Performance

- **SWE-bench Verified**: 73/73 instances resolved (100%) with diagnostic reasoning + session state, up from 93.3% (28/30) without these features.

## [1.6.0] - 2026-04-06

### Added

- **OSS/Pro split** — `reviewer`, `verifier`, `audit`, `risk` modules moved to `codd-pro` private package
- Entry-points based plugin discovery (`codd.plugins` group) — `require_plugins.py` now uses `importlib.metadata.entry_points`
- Bridge pattern in `validator.py` and `policy.py` — Pro implementations override OSS fallback when `codd-pro` is installed
- `bridge.py` — central plugin registry for Pro extensions
- `codd-dev[ai]` optional dependency group for `extract_ai.py`
- `codd-dev[mcp]` optional dependency group for `mcp_server.py`
- Graceful degradation for `review`/`verify`/`audit`/`risk` commands — shows migration message when `codd-pro` is not installed

### Removed

- `codd/reviewer.py`, `codd/verifier.py`, `codd/audit.py`, `codd/risk.py` — moved to `codd-pro`

### Migration

Users who rely on `codd review`, `codd verify`, `codd audit`, or `codd risk` should install `codd-pro`:
```
pip install "codd-pro @ git+ssh://git@github.com/yohey-w/codd-pro.git"
```
All other commands (`scan`, `generate`, `propagate`, `extract`, `validate`, `require`, `restore`, `plan`, `measure`, `impact`) are unaffected.

## [1.5.1] - 2026-04-06

### Fixed

- `codd measure` crashed with `TypeError: 'dict' object is not callable` — `ceg.nodes` is a dict attribute, not a method ([#3](https://github.com/yohey-w/codd-dev/issues/3))
- `codd validate` falsely reported `conventions.targets` nodes as "undefined" even when they existed in `nodes.jsonl` — validator now loads scan results into the known-node lookup ([#4](https://github.com/yohey-w/codd-dev/issues/4))
- `codd extract` → `codd plan --init` failed on brownfield projects because `codd.yaml` was never created — extract now auto-generates a minimal `codd.yaml` when none exists ([#2](https://github.com/yohey-w/codd-dev/issues/2))

## [1.2.1] - 2026-04-01

### Fixed

- `codd hooks install` failed with FileNotFoundError after `pip install codd-dev` — hooks/pre-commit was excluded from the wheel package ([#1](https://github.com/yohey-w/codd-dev/issues/1))
  - Moved `hooks/` into `codd/hooks/` package so it's included in wheel builds
  - Converted `codd/hooks.py` to `codd/hooks/__init__.py` package

## [1.2.0] - 2026-03-31

### Added

- `codd plan --waves` and `--sprints` flags — return counts for shell scripting (no hardcoded magic numbers)
- `codd-assemble` skill for Claude Code integration
- Assembler prompt improvement for cleaner output

### Changed

- README overhauled: split Quick Start into Greenfield/Brownfield, added 5-Minute Demos, articles section

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
