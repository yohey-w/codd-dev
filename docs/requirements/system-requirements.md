---
codd:
  node_id: req:system
  type: requirement
  depends_on: []
  confidence: 0.65
  source: codd-require
---

# System-Wide Cross-Cutting Behavior — Inferred Requirements

## 1. Overview

CoDD (Coherence-Driven Development) is a Python CLI tool (v1.3.0) that maintains bidirectional traceability between requirements, design documents, and source code. It operates in two modes: **greenfield** (requirements → design → implementation) and **brownfield** (existing code → extracted facts → inferred requirements/design). The system uses a graph-based dependency model with probabilistic confidence scoring to track cross-artifact relationships and propagate change impact.

**Evidence**: CLI entry point `codd.cli:main` via `pyproject.toml`; 28 modules totaling ~12,590 lines; dual workflow visible in `codd generate` vs `codd extract`/`codd require`/`codd restore` commands.

## 2. Functional Requirements

### 2.1 Configuration Management

- **FR-CFG-1** [observed]: The system SHALL load a default configuration from `codd/defaults.yaml` and merge it with project-local overrides from `codd/codd.yaml` or `.codd/codd.yaml`.
  - *Evidence*: `config.load_project_config()`, `config.find_codd_dir()` checking both `codd/` and `.codd/` paths.

- **FR-CFG-2** [observed]: Configuration merging SHALL use deep-merge semantics with list deduplication via JSON serialization.
  - *Evidence*: `config._deep_merge()`, `config._merge_lists()`.

- **FR-CFG-3** [observed]: The configuration SHALL define project metadata (`language`, `frameworks`), scan scope (`source_dirs`, `test_dirs`, `doc_dirs`, `exclude`), graph storage settings (`store`, `path`), confidence band thresholds (`green`, `amber`), propagation limits (`max_depth`, `stop_at_contract_boundary`), and an AI command template (`ai_command`).
  - *Evidence*: `defaults.yaml` keys enumerated in config module.

### 2.2 Dependency Graph (Conditioned Evidence Graph)

- **FR-GRAPH-1** [observed]: The system SHALL maintain a persistent graph of nodes and edges stored as JSONL files (`nodes.jsonl`, `edges.jsonl`).
  - *Evidence*: `graph.py` JSONL read/write, `close()` flush semantics.

- **FR-GRAPH-2** [observed]: Each edge SHALL accumulate multiple pieces of evidence, and confidence SHALL be computed via Noisy-OR aggregation: `1 - Π(1 - p_i)` for positive evidence, with negative evidence subtracted.
  - *Evidence*: `graph._noisy_or()`, `graph.add_evidence()` with `is_negative` flag.

- **FR-GRAPH-3** [observed]: Edges SHALL be classified into confidence bands: **green** (≥0.90 confidence AND ≥2 evidence items), **amber** (≥0.50), **gray** (below 0.50). Thresholds SHALL be configurable.
  - *Evidence*: `graph.classify_band()`, `bands.green`/`bands.amber` config keys.

- **FR-GRAPH-4** [observed]: Evidence sources SHALL be categorized as automatic (`static`, `framework`, `frontmatter`, `inferred`) or human (`human`, `dynamic`, `history`). Selective refresh SHALL purge only auto-generated evidence while preserving human-contributed evidence.
  - *Evidence*: `graph.AUTO_SOURCE_TYPES`, `graph.HUMAN_SOURCE_TYPES`, `graph.purge_auto_generated()`.

- **FR-GRAPH-5** [observed]: Impact propagation SHALL use BFS traversal over incoming edges (reverse dependency direction) up to a configurable `max_depth` (default: 10), returning affected nodes with depth and path information.
  - *Evidence*: `graph.propagate_impact()`, `propagation.max_depth` config.

### 2.3 Validation Framework

- **FR-VAL-1** [observed]: The system SHALL validate all CoDD documents for: node ID format compliance, duplicate node IDs, dangling dependency references (both `depends_on` and `depended_by`), circular dependencies, and wave_config consistency.
  - *Evidence*: `validator.py` issue codes: `invalid_node_id`, `duplicate_node_id`, `dangling_depends_on`, `dangling_depended_by`, `circular_dependency`, `wave_config_mismatch`.

- **FR-VAL-2** [observed]: Node IDs SHALL follow the format `<prefix>:<name>` where prefix is one of 33 allowed lowercase identifiers (e.g., `design`, `req`, `module`, `file`, `endpoint`, `db_table`).
  - *Evidence*: `validator.py` regex `r"^(?P<prefix>[a-z_]+):(?P<name>.+)$"` and allowed prefix list.

- **FR-VAL-3** [observed]: Validation SHALL produce structured results with three severity levels: ERROR (exit code 1), BLOCKED (planned but not yet generated), and WARNING (non-fatal).
  - *Evidence*: `ValidationResult` dataclass with `error_count`, `blocked_count`, `warning_count`, `exit_code`.

- **FR-VAL-4** [observed]: Reciprocal dependency checks SHALL warn (not error) when A depends on B but B does not list A in `depended_by`.
  - *Evidence*: `missing_depended_by` issue code at `LEVEL_WARNING`.

### 2.4 Git Hook Integration

- **FR-HOOK-1** [observed]: The system SHALL provide an installable pre-commit hook that validates staged CoDD documents before allowing commits.
  - *Evidence*: `hooks.install_pre_commit_hook()` creating symlink to `.git/hooks/pre-commit`.

- **FR-HOOK-2** [observed]: The pre-commit hook SHALL reject commits containing Markdown files in `doc_dirs` that lack valid CoDD frontmatter, and SHALL run full validation via the validator module.
  - *Evidence*: `hooks.run_pre_commit()` filtering staged `.md` files, calling `_extract_frontmatter()` and `validator.run_validate()`.

### 2.5 Traceability & Test Coverage

- **FR-TRACE-1** [observed]: The system SHALL compute per-module test coverage by matching source symbols against test file contents via substring matching.
  - *Evidence*: `traceability.build_test_traceability()`, `TestCoverage` dataclass with `covered_symbols`, `uncovered_symbols`, `coverage_ratio`.

- **FR-TRACE-2** [inferred]: Test coverage tracking is approximate — it uses name-in-text matching rather than execution-based coverage, serving as a structural heuristic rather than precise measurement.
  - *Evidence*: Substring matching strategy in `build_test_traceability()`.

### 2.6 Document Frontmatter as Data Model

- **FR-FM-1** [observed]: All CoDD documents SHALL embed structured YAML frontmatter between `---` delimiters containing at minimum `codd.node_id` and `codd.type`.
  - *Evidence*: Validator frontmatter parsing pattern `^---\s*\n(.*?)\n---`; required fields checked in validation.

- **FR-FM-2** [observed]: Frontmatter SHALL support dependency declarations (`depends_on`, `depended_by`) as either string arrays or structured objects with `id`/`node_id`, `relation`, and `semantic` fields.
  - *Evidence*: Validator reference extraction logic handling both formats.

- **FR-FM-3** [observed]: Frontmatter SHALL support optional fields: `confidence`, `status`, `modules`, `conventions`, `source`, and `last_extracted`.
  - *Evidence*: Extracted document frontmatter examples across `codd/extracted/` files.

### 2.7 Dual-Mode Workflow

- **FR-DUAL-1** [observed]: The system SHALL support a greenfield workflow: `init` → `plan` → `generate` → `implement` → `assemble` → `verify`.
  - *Evidence*: CLI commands `init`, `plan`, `generate`, `implement`, `assemble`, `verify` with wave/sprint sequencing.

- **FR-DUAL-2** [observed]: The system SHALL support a brownfield workflow: `init` → `extract` → `require`/`restore` for inferring requirements and design from existing codebases.
  - *Evidence*: CLI commands `extract` (pure structural, no AI), `require` (infer requirements), `restore` (reconstruct design).

- **FR-DUAL-3** [observed]: The `extract` command SHALL perform deterministic static analysis without requiring AI, while `generate`, `restore`, `require`, `propagate`, `implement`, and `review` MAY invoke an external AI command.
  - *Evidence*: `extract` has no `--ai-cmd` option; other commands accept `--ai-cmd` override.

### 2.8 Change Propagation

- **FR-PROP-1** [observed]: The system SHALL map git diff changes to affected modules and propagate impact to dependent design documents.
  - *Evidence*: `propagate.run_impact()` accepting `--diff` (default `HEAD`); `propagator` module coordinating config/generator/scanner.

- **FR-PROP-2** [observed]: Propagation MAY optionally update affected documents via AI when `--update` flag is provided; default behavior is analysis-only.
  - *Evidence*: CLI `propagate` command with `--update` flag.

### 2.9 AI Integration

- **FR-AI-1** [observed]: AI integration SHALL be command-line driven via a configurable `ai_command` template, defaulting to `claude --print --model claude-opus-4-6 --tools ""`.
  - *Evidence*: `ai_command` config key in `defaults.yaml`.

- **FR-AI-2** [observed]: All AI-dependent commands SHALL accept `--ai-cmd` to override the configured AI command and `--feedback` to incorporate review feedback.
  - *Evidence*: CLI options on `generate`, `restore`, `require`, `propagate`, `implement`, `review`, `plan --init`.

## 3. Non-Functional Requirements

### 3.1 Extensibility

- **NFR-EXT-1** [observed]: The parsing subsystem SHALL support multiple languages via pluggable parsers. Current implementations cover Python, TypeScript, JavaScript, Java, SQL, GraphQL, Terraform (HCL), and TOML.
  - *Evidence*: `parsing.py` (70 public symbols, 19 classes, 51 functions); external dependencies on `tree-sitter`, `tree-sitter-python`, `tree-sitter-typescript`, `tree-sitter-java`, `graphql-core`, `python-hcl2`.

- **NFR-EXT-2** [observed]: Language-specific parsers SHALL be optional dependencies installable via extras (`api-parsers`, `infra`, `tree-sitter`, `scan`).
  - *Evidence*: `pyproject.toml` optional-dependencies groups.

### 3.2 Resilience & Graceful Degradation

- **NFR-RES-1** [observed]: The extractor SHALL silently skip unreadable files and continue processing. Scanners SHALL print warnings and continue rather than aborting on individual file failures.
  - *Evidence*: Error handling patterns observed across extractor and scanner modules.

- **NFR-RES-2** [observed]: Graph storage SHALL use JSONL format for line-level diff-friendliness and resilience to partial writes.
  - *Evidence*: `graph.py` JSONL format, one record per line.

### 3.3 Git-Friendliness

- **NFR-GIT-1** [observed]: All persistent artifacts (JSONL graph files, YAML configs, Markdown documents) SHALL produce stable, deterministic output suitable for meaningful git diffs.
  - *Evidence*: JSONL line-per-record storage; YAML configuration format; Markdown document format.

- **NFR-GIT-2** [observed]: The system SHALL integrate with git workflows via pre-commit hooks and diff-based change detection.
  - *Evidence*: `hooks` module; `--diff HEAD` default in `impact` and `propagate` commands.

### 3.4 Minimal Runtime Dependencies

- **NFR-DEP-1** [observed]: Core runtime SHALL depend on only 3-4 packages: `pyyaml`, `click`, `jinja2`, and `tomli` (Python <3.11 only). All parsing libraries SHALL be optional.
  - *Evidence*: `pyproject.toml` `dependencies` vs `optional-dependencies`.

### 3.5 Observability

- **NFR-OBS-1** [inferred]: Change risk is computed per-module based on dependent count, uncovered symbols ratio, API surface area, and contract violations. This serves as a prioritization heuristic for maintenance effort.
  - *Evidence*: `risk.py` module; Change Risk Summary table in architecture overview with weighted risk scores.

- **NFR-OBS-2** [observed]: Interface contract analysis tracks public vs internal API surface and flags violations where internal symbols are accessed from outside the module.
  - *Evidence*: Interface Contracts Summary table; `cli` module flagged with 2 violations for accessing `hooks` internal symbols.

## 4. Constraints

### 4.1 Technology Stack

- **CON-TECH-1** [observed]: Python ≥3.10 is required.
  - *Evidence*: `pyproject.toml` `requires-python = ">=3.10"`.

- **CON-TECH-2** [observed]: CLI framework is Click (≥8.0). Template engine is Jinja2 (≥3.1.0). Configuration format is YAML.
  - *Evidence*: Runtime dependencies in `pyproject.toml`.

- **CON-TECH-3** [observed]: Source code parsing uses Tree-sitter bindings with pinned version ranges to avoid breaking API changes.
  - *Evidence*: `tree-sitter>=0.25.0,<0.26.0`, `tree-sitter-python>=0.25.0,<0.26.0`, `tree-sitter-typescript>=0.23.0,<0.24.0`.

### 4.2 Architectural Constraints

- **CON-ARCH-1** [observed]: The system follows a layered architecture: Presentation (CLI) → Application (assembler, generator, implementer, planner, propagator, require, restore, reviewer, verifier) → Domain (schema_refs) → Infrastructure (parsing, graph, config, extractor, scanner, etc.).
  - *Evidence*: Architecture overview layer classification.

- **CON-ARCH-2** [observed]: Two layer violations exist: `extractor` (Infrastructure) imports `schema_refs` (Domain); `scanner` (Infrastructure) imports `generator` (Application).
  - *Evidence*: Layer Violations section in architecture overview.

- **CON-ARCH-3** [observed]: The `extractor` module is a central hub with bidirectional dependencies to 10+ infrastructure modules (clustering, contracts, env_refs, inheritance, parsing, risk, schema_refs, synth, traceability, wiring), creating circular dependency chains.
  - *Evidence*: Module dependency graph showing mutual imports between `extractor` and its satellite modules.

- **CON-ARCH-4** [observed]: The system has no async code — all 28 modules use synchronous execution only.
  - *Evidence*: System context table shows `Async: 0` for every module.

### 4.3 Deployment

- **CON-DEPLOY-1** [observed]: The tool is distributed as a pip-installable Python package (`codd-dev`) with a single CLI entry point (`codd`).
  - *Evidence*: `pyproject.toml` scripts section, package name.

- **CON-DEPLOY-2** [observed]: Package build includes Python source, YAML defaults, Jinja2 templates (`.j2`, `.tmpl`), and the pre-commit hook script.
  - *Evidence*: `pyproject.toml` build includes: `codd/**/*.py`, `codd/**/*.yaml`, `codd/templates/**/*.j2`, `codd/templates/**/*.tmpl`, `codd/hooks/pre-commit`.

### 4.4 Data Format Constraints

- **CON-DATA-1** [observed]: Graph persistence is JSONL only (`graph.store: "jsonl"`). No alternative storage backends are implemented.
  - *Evidence*: Config default `graph.store: "jsonl"`; `graph.py` implements only JSONL I/O.

- **CON-DATA-2** [observed]: All CoDD metadata is embedded in document frontmatter (documents-as-data pattern). The graph is a computed index, not the source of truth.
  - *Evidence*: Frontmatter parsing across validator, hooks, and extractor; graph rebuilt from scan.

## 5. Open Questions

- **OQ-1** [review needed]: The `extractor` module has bidirectional dependencies with many infrastructure modules (e.g., `clustering ↔ extractor`, `inheritance ↔ extractor`). Is this intentional plugin-style architecture, or accidental coupling that should be refactored?

- **OQ-2** [review needed]: The two layer violations (`extractor` → `schema_refs`, `scanner` → `generator`) — are these accepted technical debt or architectural oversights?

- **OQ-3** [speculative]: The `hooks` module exposes 0 public / 2 internal symbols, yet CLI accesses both. Is the intent that hooks should only be invoked via the CLI (not imported by other modules), or should these be promoted to public API?

- **OQ-4** [review needed]: Test coverage tracking uses substring matching, which may produce false positives (e.g., a common name like `get` matching unrelated test code). Is this known approximation acceptable, or is higher-fidelity tracing planned?

- **OQ-5** [speculative]: The `ai_command` defaults to a specific Claude model invocation. Is there an abstraction boundary intended for supporting other AI backends, or is Claude coupling intentional?

- **OQ-6** [review needed]: Several modules have high change-risk scores with 100% uncovered symbols (e.g., `generator`: 0.72 risk, `validator`: 0.57, `planner`: 0.57). Is the absence of test coverage for these critical modules a known gap?

- **OQ-7** [speculative]: The `condition` field on graph edges (e.g., `"when API version >= 2.0"`) suggests conditional dependency support, but it is unclear how conditions are evaluated or enforced at runtime. Is this currently used or reserved for future use?
