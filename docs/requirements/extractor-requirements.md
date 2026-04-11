---
codd:
  node_id: req:extractor
  type: requirement
  depends_on: []
  confidence: 0.65
  source: codd-require
---

# Extractor — Inferred Requirements

## 1. Overview

The extractor module is the central fact-gathering engine of the `codd` system. It statically analyzes a software project's source code to produce a structured representation (`ProjectFacts`) of modules, symbols, call graphs, dependencies, and architectural metadata. It orchestrates ~11 subsidiary analysis passes (clustering, contracts, environment references, inheritance, parsing, risk, schema references, synthesis, traceability, and wiring) and exposes both a programmatic API and a pipeline entry point (`run_extract`) consumed by the CLI. [observed]

Evidence: `codd/extractor.py` (993 lines), import list spanning 11 internal modules, `cli -> extractor` dependency edge, `run_extract` called from CLI.

## 2. Functional Requirements

### 2.1 Project Fact Extraction

- **FR-EXT-01**: The system SHALL accept a project root path and return a `ProjectFacts` object containing all extracted structural metadata. [observed]
  - Evidence: `extract_facts(project_root: Path, ...) -> ProjectFacts` at `codd/extractor.py:129`.

- **FR-EXT-02**: The system SHALL support optional language override; when not provided, it SHALL auto-detect the project language. [observed]
  - Evidence: `language: str | None = None` parameter; test `test_language_autodetect`.

- **FR-EXT-03**: The system SHALL support filtering source directories and excluding file patterns. [observed]
  - Evidence: `source_dirs: list[str] | None`, `exclude_patterns: list[str] | None` parameters; `fnmatch` import for pattern matching.

### 2.2 Module Discovery

- **FR-EXT-04**: The system SHALL discover modules from Python projects. [observed]
  - Evidence: `_discover_modules` producing `ModuleInfo` objects; test `test_python_module_discovery`.

- **FR-EXT-05**: The system SHALL discover modules from TypeScript projects. [observed]
  - Evidence: test `test_ts_module_discovery`.

### 2.3 Symbol Extraction

- **FR-EXT-06**: The system SHALL extract symbols (classes, functions, and other named entities) from source files, producing `Symbol` objects with at least kind, name, and location. [observed]
  - Evidence: `Symbol` dataclass at line 41; `_extract_symbols` creates `Symbol` instances at 9 distinct call sites (lines 444–478); tests `test_python_symbol_extraction`, `test_ts_symbol_extraction`.

- **FR-EXT-07**: The system SHALL extract multiline signatures and decorators when using tree-sitter parsing. [observed]
  - Evidence: test `test_python_tree_sitter_extracts_multiline_signature_and_decorators`.

- **FR-EXT-08**: The system SHALL extract TypeScript-specific constructs including interfaces, type aliases, re-exports, and const objects. [observed]
  - Evidence: tests `test_typescript_tree_sitter_extracts_interfaces_aliases_and_reexports`, `test_typescript_tree_sitter_extracts_const_objects`.

### 2.4 Call Graph Construction

- **FR-EXT-09**: The system SHALL build a call graph of `CallEdge` objects representing caller-callee relationships between symbols. [observed]
  - Evidence: `CallEdge` dataclass at line 57; tests in `test_call_graph.py` and `test_ts_call_graph.py`.

- **FR-EXT-10**: The system SHALL resolve bare function names to their containing module. [observed]
  - Evidence: test `test_resolves_bare_name_to_module`.

- **FR-EXT-11**: The system SHALL detect async call edges and flag them accordingly. [observed]
  - Evidence: tests `test_async_call_detection`, `test_call_edge_is_async_flag`.

- **FR-EXT-12**: The system SHALL skip built-in function calls in the call graph. [observed]
  - Evidence: test `test_skips_builtins`.

- **FR-EXT-13**: Self-calls within the same module SHALL remain attributed to that module. [observed]
  - Evidence: test `test_self_calls_stay_local`.

### 2.5 Import and Dependency Analysis

- **FR-EXT-14**: The system SHALL build an import dependency graph between internal modules. [observed]
  - Evidence: test `test_python_import_graph`.

- **FR-EXT-15**: The system SHALL identify external (third-party) imports separately from internal ones. [observed]
  - Evidence: test `test_python_external_imports`.

### 2.6 Infrastructure and Schema Extraction

- **FR-EXT-16**: The system SHALL extract Docker Compose service definitions and Kubernetes manifests. [observed]
  - Evidence: `DockerComposeExtractor`, `KubernetesExtractor` imports from `codd.parsing`; test `test_extracts_docker_compose_and_kubernetes`.

- **FR-EXT-17**: The system SHALL extract Terraform configurations and build dependency metadata. [observed]
  - Evidence: `TerraformExtractor`, `BuildDepsExtractor` imports; test `test_extracts_terraform_build_deps_and_test_mapping`.

- **FR-EXT-18**: The system SHALL extract SQL DDL schema definitions. [observed]
  - Evidence: test `test_extract_facts_discovers_sql_ddl_schema`.

- **FR-EXT-19**: The system SHALL extract Prisma schema definitions. [observed]
  - Evidence: test `test_extract_facts_discovers_prisma_schema`.

- **FR-EXT-20**: The system SHALL gracefully skip optional infrastructure files when they are absent. [observed]
  - Evidence: test `test_gracefully_skips_when_optional_files_are_absent`.

### 2.7 API Specification Extraction

- **FR-EXT-21**: The system SHALL extract OpenAPI specifications. [observed]
  - Evidence: `OpenApiExtractor` import; test `test_extracts_openapi_specs`.

- **FR-EXT-22**: The system SHALL extract GraphQL specifications. [observed]
  - Evidence: `GraphQlExtractor` import; test `test_extracts_graphql_specs`.

- **FR-EXT-23**: The system SHALL extract Protobuf specifications. [observed]
  - Evidence: `ProtobufExtractor` import; test `test_extracts_protobuf_specs`.

- **FR-EXT-24**: The system SHALL skip API extraction silently for projects without API spec files. [observed]
  - Evidence: test `test_skips_projects_without_api_specs`.

### 2.8 Cross-Cutting Analysis Passes

- **FR-EXT-25**: The system SHALL compute feature clusters by grouping related modules. [observed]
  - Evidence: `from codd.clustering import build_feature_clusters`; `FeatureCluster` dataclass at line 86.

- **FR-EXT-26**: The system SHALL build interface contracts (public vs. internal API surface). [observed]
  - Evidence: `from codd.contracts import build_interface_contracts`.

- **FR-EXT-27**: The system SHALL catalog environment variable references. [observed]
  - Evidence: `from codd.env_refs import build_env_refs`.

- **FR-EXT-28**: The system SHALL build class inheritance trees. [observed]
  - Evidence: `from codd.inheritance import build_inheritance_tree`.

- **FR-EXT-29**: The system SHALL compute change risk scores per module. [observed]
  - Evidence: `from codd.risk import build_change_risks`.

- **FR-EXT-30**: The system SHALL extract schema cross-references (foreign keys, indexes). [observed]
  - Evidence: `from codd.schema_refs import build_schema_refs`.

- **FR-EXT-31**: The system SHALL build test-to-module traceability mappings. [observed]
  - Evidence: `from codd.traceability import build_test_traceability`; test `test_python_test_mapping`.

- **FR-EXT-32**: The system SHALL detect runtime wiring patterns (DI, event buses, etc.). [inferred]
  - Evidence: `from codd.wiring import build_runtime_wires`.

### 2.9 Document Synthesis

- **FR-EXT-33**: The system SHALL generate per-module Markdown design documents from `ProjectFacts`. [observed]
  - Evidence: `synth_docs(facts, output_dir) -> list[Path]`; test `test_generates_module_docs`.

- **FR-EXT-34**: The system SHALL generate a system-context overview document. [observed]
  - Evidence: test `test_generates_system_context`.

- **FR-EXT-35**: The system SHALL generate an architecture overview that classifies modules into layers and flags layer violations. [observed]
  - Evidence: `synth_architecture(facts, output_dir) -> Path`; test `test_synth_architecture_classifies_layers_and_flags_violations`.

- **FR-EXT-36**: Generated documents SHALL include YAML frontmatter with dependency metadata (`depends_on`). [observed]
  - Evidence: test `test_frontmatter_has_depends_on`.

- **FR-EXT-37**: Synthesized module detail documents SHALL include API routes and async function annotations. [observed]
  - Evidence: test `test_module_detail_includes_api_routes_and_async_functions`.

- **FR-EXT-38**: Schema design documents SHALL render foreign keys and indexes. [observed]
  - Evidence: test `test_schema_design_renders_foreign_keys_and_indexes`.

- **FR-EXT-39**: API contract documents SHALL render OpenAPI endpoints. [observed]
  - Evidence: test `test_api_contract_renders_openapi_endpoints`.

### 2.10 Framework Detection

- **FR-EXT-40**: The system SHALL detect frameworks used in Python projects. [observed]
  - Evidence: test `test_framework_detection`.

- **FR-EXT-41**: The system SHALL detect frameworks used in TypeScript projects. [observed]
  - Evidence: test `test_ts_framework_detection`.

### 2.11 Pipeline Entry Point

- **FR-EXT-42**: The system SHALL provide a `run_extract` function that orchestrates fact extraction followed by document synthesis, returning an `ExtractResult`. [observed]
  - Evidence: `run_extract` calls `extract_facts` then `synth_docs` (lines 967, 976), returns `ExtractResult` (line 978).

- **FR-EXT-43**: The system SHALL support a custom output directory for synthesized documents. [observed]
  - Evidence: `output: str | None` parameter on `run_extract`; test `test_custom_output_dir`.

- **FR-EXT-44**: The system SHALL function without a prior `codd init` (no `.codd/` directory required). [observed]
  - Evidence: test `test_works_without_codd_init`.

- **FR-EXT-45**: The `extract` command SHALL be registered as a CLI subcommand. [observed]
  - Evidence: test `test_extract_command_exists`.

### 2.12 Parsing Strategy

- **FR-EXT-46**: The system SHALL use tree-sitter for precise AST-based extraction when available. [observed]
  - Evidence: `tree_sitter`, `tree_sitter_python`, `tree_sitter_typescript` imports; tree-sitter-specific tests.

- **FR-EXT-47**: The system SHALL fall back to regex-based extraction when tree-sitter is unavailable. [observed]
  - Evidence: test `test_extract_facts_falls_back_to_regex_when_tree_sitter_is_unavailable`; test `test_regex_fallback_returns_empty`.

- **FR-EXT-48**: The regex fallback for call graph extraction SHALL return empty results (no false positives). [observed]
  - Evidence: tests `test_regex_extractor_call_graph_returns_empty_python`, `test_regex_extractor_call_graph_returns_empty_typescript`, `test_regex_extractor_call_graph_returns_empty_javascript`.

### 2.13 Line Counting

- **FR-EXT-49**: The system SHALL count lines of code per module. [observed]
  - Evidence: test `test_line_counting`.

### 2.14 Confidence Scoring

- **FR-EXT-50**: Generated documents SHALL include a confidence score; low-confidence extractions SHALL be flagged. [observed]
  - Evidence: test `test_confidence_below_green`; frontmatter `confidence: 0.75` in extracted docs.

## 3. Non-Functional Requirements

- **NFR-EXT-01**: The extraction pipeline SHALL be synchronous (no async execution). [observed]
  - Evidence: all call graph entries show `Async: no`; 0 async functions in module map.

- **NFR-EXT-02**: The extractor SHALL maintain high test coverage (90% symbol coverage observed). [observed]
  - Evidence: coverage 0.9 (9/10 symbols covered) across 8 test files.

- **NFR-EXT-03**: The extractor SHALL be resilient to missing optional files and dependencies, degrading gracefully rather than failing. [observed]
  - Evidence: `test_gracefully_skips_when_optional_files_are_absent`, `test_skips_projects_without_api_specs`, regex fallback behavior.

- **NFR-EXT-04**: The system SHALL support multi-language extraction (at minimum Python, TypeScript, and JavaScript). [observed]
  - Evidence: separate test suites for Python and TypeScript; JavaScript call graph tests; tree-sitter grammars for all three.

## 4. Constraints

- **C-EXT-01**: Implementation language is Python. [observed]
  - Evidence: `codd/extractor.py`, `pyproject.toml`.

- **C-EXT-02**: Tree-sitter grammars are development/optional dependencies, not runtime requirements. [observed]
  - Evidence: tree-sitter packages listed under development dependencies in `pyproject.toml`; regex fallback exists.

- **C-EXT-03**: YAML (`pyyaml`) and `fnmatch` are used for configuration and pattern matching respectively. [observed]
  - Evidence: external dependencies list.

- **C-EXT-04**: The extractor depends on 11 internal modules (clustering, config, contracts, env_refs, inheritance, parsing, risk, schema_refs, synth, traceability, wiring), making it the highest fan-out module in the system. [observed]
  - Evidence: dependency graph shows `extractor` with 11 outgoing edges; dependents score 1.0 in risk table.

- **C-EXT-05**: Bidirectional dependencies exist between `extractor` and several infrastructure modules (clustering, contracts, env_refs, inheritance, parsing, risk, schema_refs, synth, traceability, wiring). [observed]
  - Evidence: dependency graph shows edges in both directions (e.g., `extractor -> clustering` and `clustering -> extractor`). These are likely type-level imports for data classes rather than circular call chains. [inferred]

- **C-EXT-06**: The extractor imports `schema_refs` (Domain layer) from Infrastructure layer, which is flagged as a layer violation. [observed]
  - Evidence: architecture overview layer violations section.

## 5. Open Questions

- **OQ-EXT-01**: The `FeatureCluster` class (line 86) has zero test coverage. Is this intentional or an oversight? [observed — needs human confirmation]

- **OQ-EXT-02**: The bidirectional dependencies between `extractor` and 10+ modules suggest the data classes (`Symbol`, `ModuleInfo`, etc.) may be shared types that belong in a separate shared/domain module. Was colocation in `extractor.py` a deliberate design choice or organic growth? [speculative — needs human confirmation]

- **OQ-EXT-03**: Java tree-sitter grammar is listed as a development dependency (`tree-sitter-java>=0.22`), but no Java-specific extraction tests exist. Is Java extraction implemented, partially implemented, or planned? [observed — needs human confirmation]

- **OQ-EXT-04**: The regex fallback deliberately returns empty call graphs for all languages. Is this because partial/noisy results were deemed worse than no results, or is regex call graph extraction simply not yet implemented? [speculative — needs human confirmation]

- **OQ-EXT-05**: SQL tree-sitter grammar (`tree_sitter_sql`) is imported but not listed in `pyproject.toml` dependencies. How is it installed? [observed — needs human confirmation]
