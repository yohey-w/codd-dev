---
codd:
  node_id: design:extract:extractor
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  source_files:
  - codd/extractor.py
  depends_on:
  - id: design:extract:clustering
    relation: imports
    semantic: technical
  - id: design:extract:config
    relation: imports
    semantic: technical
  - id: design:extract:contracts
    relation: imports
    semantic: technical
  - id: design:extract:parsing
    relation: imports
    semantic: technical
  - id: design:extract:risk
    relation: imports
    semantic: technical
  - id: design:extract:schema-refs
    relation: imports
    semantic: technical
  - id: design:extract:synth
    relation: imports
    semantic: technical
  - id: design:extract:traceability
    relation: imports
    semantic: technical
  - id: design:extract:wiring
    relation: imports
    semantic: technical
---
# extractor

> 1 files, 983 lines

**Layer Guess**: Infrastructure
**Responsibility**: Implements parsing, extraction, scanning, or adapters

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `Symbol` | `codd/extractor.py:41` | — |
| class | `CallEdge` | `codd/extractor.py:57` | — |
| class | `ModuleInfo` | `codd/extractor.py:66` | — |
| class | `FeatureCluster` | `codd/extractor.py:85` | — |
| class | `ProjectFacts` | `codd/extractor.py:94` | — |
| class | `ExtractResult` | `codd/extractor.py:114` | — |
| function | `extract_facts` | `codd/extractor.py:127` | `extract_facts(project_root: Path, language: str | None = None, source_dirs: list[str] | None = None, exclude_patterns: list[str] | None = None) -> ProjectFacts` |
| function | `synth_docs` | `codd/extractor.py:921` | `synth_docs(facts: ProjectFacts, output_dir: Path) -> list[Path]` |
| function | `synth_architecture` | `codd/extractor.py:928` | `synth_architecture(facts: ProjectFacts, output_dir: Path) -> Path` |
| function | `run_extract` | `codd/extractor.py:939` | `run_extract(project_root: Path, language: str | None = None, source_dirs: list[str] | None = None, output: str | None = None) -> ExtractResult` |






## Public API

- `Symbol`
- `CallEdge`
- `ModuleInfo`
- `FeatureCluster`
- `ProjectFacts`
- `ExtractResult`
- `extract_facts`
- `synth_docs`
- `synth_architecture`
- `run_extract`

## Call Graph

| Caller | Callee | Location | Async |
|--------|--------|----------|-------|
| `extract_facts` | `ProjectFacts` | `codd/extractor.py:147` | no |
| `_discover_modules` | `ModuleInfo` | `codd/extractor.py:312` | no |
| `_extract_symbols` | `Symbol` | `codd/extractor.py:434` | no |
| `_extract_symbols` | `Symbol` | `codd/extractor.py:437` | no |
| `_extract_symbols` | `Symbol` | `codd/extractor.py:443` | no |
| `_extract_symbols` | `Symbol` | `codd/extractor.py:446` | no |
| `_extract_symbols` | `Symbol` | `codd/extractor.py:450` | no |
| `_extract_symbols` | `Symbol` | `codd/extractor.py:456` | no |
| `_extract_symbols` | `Symbol` | `codd/extractor.py:459` | no |
| `_extract_symbols` | `Symbol` | `codd/extractor.py:465` | no |
| `_extract_symbols` | `Symbol` | `codd/extractor.py:468` | no |
| `run_extract` | `extract_facts` | `codd/extractor.py:957` | no |
| `run_extract` | `synth_docs` | `codd/extractor.py:966` | no |
| `run_extract` | `ExtractResult` | `codd/extractor.py:968` | no |

## Test Coverage

**Coverage**: 0.9 (9 / 10)
Tests: tests/test_api_extractor.py, tests/test_call_graph.py, tests/test_ddl_extractor.py, tests/test_extract.py, tests/test_infra_extractor.py, tests/test_synth_templates.py, tests/test_tree_sitter_extractor.py

**Uncovered symbols**: `FeatureCluster`


## Import Dependencies

### → clustering

- `from codd.clustering import build_feature_clusters`
### → config

- `from codd.config import find_codd_dir`
### → contracts

- `from codd.contracts import build_interface_contracts`
### → parsing

- `from codd.parsing import ( BuildDepsExtractor, BuildDepsInfo, ConfigInfo, DockerComposeExtractor, GraphQlExtractor, KubernetesExtractor, OpenApiExtractor, ProtobufExtractor, TerraformExtractor, TestExtractor, TestInfo, get_extractor, )`
### → risk

- `from codd.risk import build_change_risks`
### → schema_refs

- `from codd.schema_refs import build_schema_refs`
### → synth

- `from codd.synth import synth_docs as synth_docs_impl`
- `from codd.synth import synth_architecture as synth_architecture_impl`
### → traceability

- `from codd.traceability import build_test_traceability`
### → wiring

- `from codd.wiring import build_runtime_wires`

## External Dependencies

- `fnmatch`
- `yaml`

## Files

- `codd/extractor.py`

## Tests

- `tests/test_call_graph.py` — tests: test_resolves_bare_name_to_module, test_self_calls_stay_local, test_unknown_callee_unchanged, test_extracts_function_calls, test_skips_builtins, test_async_call_detection, test_regex_fallback_returns_empty- `tests/test_extract.py` — tests: test_authenticate, test_user_model, test_python_module_discovery, test_python_symbol_extraction, test_python_import_graph, test_python_external_imports, test_python_test_mapping, test_framework_detection, test_line_counting, test_language_autodetect, test_ts_module_discovery, test_ts_symbol_extraction, test_ts_framework_detection, test_generates_system_context, test_generates_module_docs, test_frontmatter_has_depends_on, test_confidence_below_green, test_full_pipeline, test_works_without_codd_init, test_custom_output_dir, test_extract_command_exists, test_extract_on_project; fixtures: python_project, ts_project- `tests/test_synth_templates.py` — tests: test_status, test_synth_docs_renders_system_context_and_architecture, test_module_detail_includes_api_routes_and_async_functions, test_schema_design_renders_foreign_keys_and_indexes, test_api_contract_renders_openapi_endpoints, test_synth_architecture_classifies_layers_and_flags_violations- `tests/test_infra_extractor.py` — tests: test_handler, test_extracts_docker_compose_and_kubernetes, test_extracts_terraform_build_deps_and_test_mapping, test_gracefully_skips_when_optional_files_are_absent; fixtures: sample_client- `tests/test_ddl_extractor.py` — tests: test_extract_facts_discovers_sql_ddl_schema, test_extract_facts_discovers_prisma_schema- `tests/test_tree_sitter_extractor.py` — tests: test_python_tree_sitter_extracts_multiline_signature_and_decorators, test_typescript_tree_sitter_extracts_interfaces_aliases_and_reexports, test_extract_facts_falls_back_to_regex_when_tree_sitter_is_unavailable- `tests/test_api_extractor.py` — tests: test_extracts_openapi_specs, test_extracts_graphql_specs, test_extracts_protobuf_specs, test_skips_projects_without_api_specs