---
codd:
  node_id: design:extract:extractor
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  depends_on:
  - id: design:extract:config
    relation: imports
    semantic: technical
  - id: design:extract:parsing
    relation: imports
    semantic: technical
  - id: design:extract:synth
    relation: imports
    semantic: technical
---
# extractor

> 1 files, 886 lines

**Layer Guess**: Infrastructure
**Responsibility**: Implements parsing, extraction, scanning, or adapters

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| class | `Symbol` | `codd/extractor.py:41` | — |
| class | `ModuleInfo` | `codd/extractor.py:57` | — |
| class | `ProjectFacts` | `codd/extractor.py:71` | — |
| class | `ExtractResult` | `codd/extractor.py:89` | — |
| function | `extract_facts` | `codd/extractor.py:102` | `extract_facts(project_root: Path, language: str | None = None, source_dirs: list[str] | None = None, exclude_patterns: list[str] | None = None) -> ProjectFacts` |
| function | `synth_docs` | `codd/extractor.py:824` | `synth_docs(facts: ProjectFacts, output_dir: Path) -> list[Path]` |
| function | `synth_architecture` | `codd/extractor.py:831` | `synth_architecture(facts: ProjectFacts, output_dir: Path) -> Path` |
| function | `run_extract` | `codd/extractor.py:842` | `run_extract(project_root: Path, language: str | None = None, source_dirs: list[str] | None = None, output: str | None = None) -> ExtractResult` |






## Import Dependencies

### → config

- `from codd.config import find_codd_dir`
### → parsing

- `from codd.parsing import ( BuildDepsExtractor, BuildDepsInfo, ConfigInfo, DockerComposeExtractor, GraphQlExtractor, KubernetesExtractor, OpenApiExtractor, ProtobufExtractor, TerraformExtractor, TestExtractor, TestInfo, get_extractor, )`
### → synth

- `from codd.synth import synth_docs as synth_docs_impl`
- `from codd.synth import synth_architecture as synth_architecture_impl`

## External Dependencies

- `fnmatch`
- `yaml`

## Files

- `codd/extractor.py`

## Tests

- `tests/test_extract.py` — tests: test_authenticate, test_user_model, test_python_module_discovery, test_python_symbol_extraction, test_python_import_graph, test_python_external_imports, test_python_test_mapping, test_framework_detection, test_line_counting, test_language_autodetect, test_ts_module_discovery, test_ts_symbol_extraction, test_ts_framework_detection, test_generates_system_context, test_generates_module_docs, test_frontmatter_has_depends_on, test_confidence_below_green, test_full_pipeline, test_works_without_codd_init, test_custom_output_dir, test_extract_command_exists, test_extract_on_project; fixtures: python_project, ts_project- `tests/test_synth_templates.py` — tests: test_status, test_synth_docs_renders_system_context_and_architecture, test_module_detail_includes_api_routes_and_async_functions, test_schema_design_renders_foreign_keys_and_indexes, test_api_contract_renders_openapi_endpoints, test_synth_architecture_classifies_layers_and_flags_violations- `tests/test_infra_extractor.py` — tests: test_handler, test_extracts_docker_compose_and_kubernetes, test_extracts_terraform_build_deps_and_test_mapping, test_gracefully_skips_when_optional_files_are_absent; fixtures: sample_client- `tests/test_ddl_extractor.py` — tests: test_extract_facts_discovers_sql_ddl_schema, test_extract_facts_discovers_prisma_schema- `tests/test_tree_sitter_extractor.py` — tests: test_python_tree_sitter_extracts_multiline_signature_and_decorators, test_typescript_tree_sitter_extracts_interfaces_aliases_and_reexports, test_extract_facts_falls_back_to_regex_when_tree_sitter_is_unavailable- `tests/test_api_extractor.py` — tests: test_extracts_openapi_specs, test_extracts_graphql_specs, test_extracts_protobuf_specs, test_skips_projects_without_api_specs