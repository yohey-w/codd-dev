---
codd:
  node_id: design:extract:cli
  type: design
  source: extracted
  confidence: 0.75
  last_extracted: '2026-03-30'
  depends_on:
  - id: design:extract:config
    relation: imports
    semantic: technical
  - id: design:extract:extractor
    relation: imports
    semantic: technical
  - id: design:extract:generator
    relation: imports
    semantic: technical
  - id: design:extract:hooks
    relation: imports
    semantic: technical
  - id: design:extract:implementer
    relation: imports
    semantic: technical
  - id: design:extract:planner
    relation: imports
    semantic: technical
  - id: design:extract:propagate
    relation: imports
    semantic: technical
  - id: design:extract:scanner
    relation: imports
    semantic: technical
  - id: design:extract:validator
    relation: imports
    semantic: technical
  - id: design:extract:verifier
    relation: imports
    semantic: technical
---
# cli

> 1 files, 433 lines

**Layer Guess**: Presentation
**Responsibility**: Exposes routes, handlers, or CLI entrypoints

## Symbol Inventory

| Kind | Name | Location | Signature / Notes |
|------|------|----------|-------------------|
| function | `main` | `codd/cli.py:25` | `main()` |
| function | `init` | `codd/cli.py:45` | `init(project_name: str, language: str, dest: str, requirements: str | None, config_dir: str)` |
| function | `scan` | `codd/cli.py:96` | `scan(path: str)` |
| function | `impact` | `codd/cli.py:109` | `impact(diff: str, path: str, output: str)` |
| function | `generate` | `codd/cli.py:127` | `generate(wave: int, path: str, force: bool, ai_cmd: str | None)` |
| function | `implement` | `codd/cli.py:176` | `implement(sprint: int, path: str, task: str | None, ai_cmd: str | None)` |
| function | `verify` | `codd/cli.py:202` | `verify(path: str, sprint: int | None) -> None` |
| function | `extract` | `codd/cli.py:250` | `extract(path: str, language: str | None, source_dirs: str | None, output: str | None)` |
| function | `validate` | `codd/cli.py:283` | `validate(path: str)` |
| function | `plan` | `codd/cli.py:303` | `plan(path: str, as_json: bool, initialize: bool, force: bool, ai_cmd: str | None)` |
| function | `hooks` | `codd/cli.py:352` | `hooks()` |
| function | `hooks_install` | `codd/cli.py:359` | `hooks_install(path: str)` |
| function | `hooks_run_pre_commit` | `codd/cli.py:379` | `hooks_run_pre_commit(path: str)` |






## Import Dependencies

### → config

- `from codd.config import find_codd_dir`
### → extractor

- `from codd.extractor import run_extract`
### → generator

- `from codd.generator import generate_wave, _load_project_config`
### → hooks

- `from codd.hooks import install_pre_commit_hook`
- `from codd.hooks import run_pre_commit`
### → implementer

- `from codd.implementer import implement_sprint`
### → planner

- `from codd.planner import plan_init`
- `from codd.planner import build_plan, plan_init, plan_to_dict, render_plan_text`
### → propagate

- `from codd.propagate import run_impact`
### → scanner

- `from codd.scanner import run_scan`
### → validator

- `from codd.validator import run_validate`
### → verifier

- `from codd.verifier import VerifyPreflightError, run_verify`

## External Dependencies

- `click`
- `shutil`

## Files

- `codd/cli.py`

## Tests

- `tests/test_generate.py` — tests: test_generate_command_creates_wave_documents_from_config, test_generate_frontmatter_infers_depended_by_and_inherits_conventions, test_generate_skips_existing_files_without_force, test_generate_force_overwrites_existing_files, test_generate_uses_dependency_documents_as_ai_context, test_generate_supports_detailed_design_documents_with_mermaid_guidance, test_generate_command_allows_ai_cmd_override, test_sanitize_generated_body_removes_meta_preamble_code_fence_and_duplicate_title, test_sanitize_generated_body_removes_leading_meta_line_without_heading, test_sanitize_generated_body_removes_inline_context_meta_preamble, test_sanitize_generated_body_removes_docs_directory_meta_preamble, test_sanitize_generated_body_removes_heres_meta_preamble, test_sanitize_generated_body_removes_meta_line_and_duplicate_title_after_heading, test_sanitize_generated_body_removes_meta_line_inside_body_section, test_sanitize_generated_body_removes_codex_existing_file_meta_block, test_sanitize_generated_body_removes_japanese_created_file_meta_line, test_sanitize_generated_body_rejects_unstructured_detailed_design_summary, test_sanitize_generated_body_rejects_detailed_design_without_mermaid; fixtures: mock_ai_cli- `tests/test_implement.py` — tests: test_implement_command_generates_files_with_traceability_comments, test_implement_falls_back_to_milestone_inference_for_sprint_one, test_implement_includes_detailed_design_dependency_documents_in_prompt; fixtures: mock_implement_ai- `tests/test_verify.py` — tests: test_run_verify_pass, test_run_verify_typecheck_fail, test_run_verify_test_fail, test_preflight_check_missing_package_json, test_extract_design_refs, test_extract_design_refs_missing_header, test_verify_cli_reports_propagate_targets- `tests/test_validate.py` — tests: test_validate_error_when_frontmatter_missing, test_validate_error_when_depends_on_dangles, test_validate_marks_wave_config_forward_reference_as_blocked, test_validate_marks_missing_wave_config_output_as_blocked, test_validate_warns_for_requirement_references_to_implementation_phase_nodes, test_validate_error_when_cycle_exists, test_validate_ok_when_documents_are_consistent, test_validate_allows_plan_and_operations_node_prefixes, test_validate_error_when_wave_config_mismatches_depends_on, test_validate_cli_reports_ok_status- `tests/test_plan.py` — tests: test_plan_marks_only_first_wave_ready_when_no_outputs_exist, test_plan_init_prompt_mentions_detailed_design_wave, test_plan_init_accepts_detailed_design_artifacts_from_ai, test_plan_marks_next_wave_ready_when_previous_wave_is_done, test_plan_keeps_next_wave_blocked_until_all_dependencies_are_done, test_plan_marks_all_waves_done_when_all_outputs_validate, test_plan_keeps_wave_config_forward_reference_out_of_error_state, test_plan_still_marks_existing_invalid_artifact_as_error_for_unknown_reference, test_plan_command_json_output_has_expected_schema, test_plan_command_init_generates_wave_config_from_requirement_docs, test_parse_wave_config_output_ignores_leading_summary_before_yaml, test_parse_wave_config_output_ignores_summary_and_trailing_code_fence, test_plan_command_init_prompts_before_overwriting_existing_wave_config, test_plan_command_init_force_overwrites_existing_wave_config; fixtures: mock_plan_init_ai