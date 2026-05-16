---
codd:
  node_id: req:cli
  type: requirement
  depends_on: []
  confidence: 0.65
  source: codd-require
---

# CLI Requirements (Inferred from Codebase)

## 1. Overview

The `cli` module is the single presentation-layer entry point for the **CoDD (Coherence-Driven Development)** tool. It exposes a `click`-based command-line interface that orchestrates all system capabilities — project initialization, static code analysis, AI-driven document generation, change propagation, implementation scaffolding, verification, and Git hook integration. The CLI delegates all domain logic to dedicated application and infrastructure modules, acting purely as a routing and user-interaction layer.

Evidence: `codd/cli.py` (705 lines, 18 public symbols), registered as entry point `codd` in `pyproject.toml`. [observed]

## 2. Functional Requirements

### FR-CLI-01: Project Initialization (`codd init`) [observed]
- The CLI **shall** prompt the user for project name and primary language when not supplied as options. (Evidence: `click.option("--project-name", prompt="Project name")` at `cli.py:31`)
- The CLI **shall** accept `--dest` (default `.`) and `--config-dir` (default `codd`) to control where the CoDD directory is created. (Evidence: `cli.py:33-43`)
- The CLI **shall** create the directory structure `<config-dir>/`, `<config-dir>/reports/`, and `<config-dir>/scan/`. (Evidence: `cli.py:62-63`)
- The CLI **shall** render `codd.yaml` and `.gitignore` from template files shipped in `codd/templates/`. (Evidence: `cli.py:66-70`, `TEMPLATES_DIR` at line 11)
- The CLI **shall** write a `.codd_version` file containing the version string `0.2.0`. (Evidence: `cli.py:73`)
- The CLI **shall** refuse to initialize if the config directory already exists, unless `--requirements` is provided (in which case only the import is performed). (Evidence: `cli.py:50-59`)

### FR-CLI-02: Requirements Import (`codd init --requirements`) [observed]
- The CLI **shall** accept an existing file path via `--requirements` and copy it into `docs/requirements/requirements.md`. (Evidence: `_import_requirements` at `cli.py:673-701`)
- The CLI **shall** auto-generate CoDD YAML frontmatter (with `node_id`, `type: requirement`, `status: approved`, `confidence: 0.95`) if the source file lacks it. (Evidence: `cli.py:682-694`)
- The node_id **shall** be derived from the project name by slugifying it (`req:<slug>-requirements`). (Evidence: `cli.py:684`)

### FR-CLI-03: Codebase Scanning (`codd scan`) [observed]
- The CLI **shall** delegate to `scanner.run_scan` to scan the codebase and update the dependency graph. (Evidence: `cli.py:96-102`)
- The CLI **shall** require an initialized CoDD directory. (Evidence: `_require_codd_dir` call at `cli.py:100`)

### FR-CLI-04: Change Impact Analysis (`codd impact`) [observed]
- The CLI **shall** accept a `--diff` target (default `HEAD`) and analyze change impact from a git diff. (Evidence: `cli.py:106-115`)
- The CLI **shall** accept an optional `--output` file path (default: stdout). (Evidence: `cli.py:108`)

### FR-CLI-05: Wave-Based Document Generation (`codd generate`) [observed]
- The CLI **shall** require a `--wave` number (integer >= 1). (Evidence: `cli.py:119`)
- The CLI **shall** auto-generate `wave_config` via `planner.plan_init` if it is missing from the project config, before proceeding with generation. (Evidence: `cli.py:136-146`)
- The CLI **shall** support `--force` to overwrite existing files, `--ai-cmd` to override the AI CLI command, and `--feedback` to incorporate review feedback. (Evidence: `cli.py:121-127`)
- The CLI **shall** report per-file status (generated/skipped) and a wave-level summary. (Evidence: `cli.py:157-165`)

### FR-CLI-06: Design Document Restoration — Brownfield (`codd restore`) [observed]
- The CLI **shall** reconstruct design documents from extracted codebase facts, as opposed to generating from requirements (greenfield). (Evidence: `cli.py:179-187`, docstring)
- The CLI **shall** accept the same `--wave`, `--force`, `--ai-cmd`, and `--feedback` options as `generate`. (Evidence: `cli.py:169-177`)

### FR-CLI-07: Requirements Inference — Brownfield (`codd require`) [observed]
- The CLI **shall** reverse-engineer requirements documents from extracted code analysis. (Evidence: `cli.py:225-229`)
- The CLI **shall** accept `--output` (default `docs/requirements/`), `--scope` to limit to a specific service boundary, `--force`, `--ai-cmd`, and `--feedback`. (Evidence: `cli.py:215-223`)

### FR-CLI-08: Change Propagation (`codd propagate`) [observed]
- The CLI **shall** detect changed source files from git diff, map them to modules, and identify affected design documents via the `modules` frontmatter field. (Evidence: `cli.py:277-284`, `cli.py:296-321`)
- Without `--update`: the CLI **shall** perform analysis only and show affected docs. (Evidence: `cli.py:320-321`)
- With `--update`: the CLI **shall** call AI to update each affected design document. (Evidence: `cli.py:269`, `propagator.run_propagate`)

### FR-CLI-09: Sprint Implementation (`codd implement`) [observed]
- The CLI **shall** require a `--sprint` number and generate implementation code for that sprint. (Evidence: `cli.py:325-333`)
- The CLI **shall** support `--task` to generate only a single task by ID or title match. (Evidence: `cli.py:327`)
- The CLI **shall** report generated file paths with their task IDs. (Evidence: `cli.py:347-353`)

### FR-CLI-10: Project Assembly (`codd assemble`) [observed]
- The CLI **shall** assemble generated sprint fragments into a working project in an output directory (default: `src/`). (Evidence: `cli.py:356-377`)

### FR-CLI-11: Build & Test Verification (`codd verify`) [observed]
- The CLI **shall** run typecheck and test verification, reporting PASS/FAIL status with error counts. (Evidence: `cli.py:399-407`)
- The CLI **shall** trace test failures to design documents and suggest propagate targets. (Evidence: `cli.py:409-417`)
- The CLI **shall** accept an optional `--sprint` to scope verification. (Evidence: `cli.py:382`)
- The CLI **shall** exit with code 0 on success, 1 on failure. (Evidence: `cli.py:423`)
- The CLI **shall** handle `VerifyPreflightError` separately from other errors. (Evidence: `cli.py:393-394`)

### FR-CLI-12: Codebase Extraction (`codd extract`) [observed]
- The CLI **shall** perform pure structural fact extraction from source code without AI. (Evidence: docstring at `cli.py:432-438`)
- The CLI **shall** support `--language` override, `--source-dirs` as comma-separated list, and `--output` directory (default: `codd/extracted/`). (Evidence: `cli.py:427-430`)
- The CLI **shall** report module count, file count, line count, and list generated files. (Evidence: `cli.py:451-454`)

### FR-CLI-13: Reserved
- The legacy AI-powered document review command was removed in v2.19.0 because no implementation body existed in the OSS package.

### FR-CLI-14: Frontmatter Validation (`codd validate`) [observed]
- The CLI **shall** validate CoDD frontmatter and dependency references across all documents. (Evidence: `cli.py:536`)
- The CLI **shall** exit with the return code from `run_validate`. (Evidence: `cli.py:542`)

### FR-CLI-15: Wave Execution Planning (`codd plan`) [observed]
- The CLI **shall** show wave execution status from configured artifacts. (Evidence: `cli.py:558`)
- With `--init`: the CLI **shall** generate `wave_config` from requirement documents using AI, with confirmation prompt if wave_config already exists (unless `--force`). (Evidence: `cli.py:564-591`)
- With `--json`: the CLI **shall** output the plan as JSON. (Evidence: `cli.py:616-618`)
- With `--waves`: the CLI **shall** output only the total wave count (for shell scripting). (Evidence: `cli.py:598-603`)
- With `--sprints`: the CLI **shall** output only the total sprint count (for shell scripting). (Evidence: `cli.py:605-608`)
- The CLI **shall** enforce mutual exclusion: `--json` cannot be used with `--init`; `--force` requires `--init`; `--ai-cmd` requires `--init` (or `--waves`/`--sprints`). (Evidence: `cli.py:565-596`)

### FR-CLI-16: Git Hook Management (`codd hooks`) [observed]
- The CLI **shall** provide a `hooks` subgroup with `install` and `run-pre-commit` subcommands. (Evidence: `cli.py:623-656`)
- `hooks install` **shall** install a pre-commit hook into `.git/hooks/`. (Evidence: `cli.py:631-646`)
- `hooks run-pre-commit` **shall** be a hidden command that runs CoDD pre-commit checks. (Evidence: `cli.py:649`, `hidden=True`)

### FR-CLI-17: AI Command Resolution [observed]
- All AI-dependent commands **shall** accept `--ai-cmd` to override the configured AI CLI command. (Evidence: consistent `--ai-cmd` option across `generate`, `restore`, `require`, `propagate`, `implement`, `assemble`, `plan`)
- [inferred] Resolution priority: CLI `--ai-cmd` > per-command `ai_commands` in `codd.yaml` > global `ai_command` in `codd.yaml` > default `'claude --print'`. (Evidence: test names `test_resolve_ai_command_*` in `test_generate.py`)

### FR-CLI-18: Feedback Loop Integration [observed]
- The `generate`, `restore`, `require`, and `propagate` commands **shall** accept a `--feedback` option to incorporate review feedback into AI prompts. (Evidence: `cli.py:127, 177, 223, 275`; tests in `test_feedback_loop.py`)

### FR-CLI-19: Version Display [observed]
- The CLI **shall** support `--version` derived from the `codd-dev` package metadata. (Evidence: `@click.version_option(package_name="codd-dev")` at `cli.py:24`)

## 3. Non-Functional Requirements

### NFR-CLI-01: Lazy Imports [observed]
- All heavy module imports **shall** be deferred to within command functions (not at module top level) to minimize CLI startup time. (Evidence: every command body contains `from codd.xxx import ...` inside the function scope, e.g., `cli.py:98, 111, 131, 188`)

### NFR-CLI-02: Consistent Error Handling [observed]
- All commands **shall** catch `FileNotFoundError` and `ValueError` from delegated modules and print user-friendly error messages before exiting with code 1. (Evidence: try/except pattern repeated in every command)

### NFR-CLI-03: Exit Code Discipline [observed]
- Commands **shall** exit with code 0 on success and code 1 on failure. Verification commands **shall** use `SystemExit` to propagate the exit code. (Evidence: `cli.py:423, 542, 656`)

### NFR-CLI-04: Test Coverage [observed]
- 12 of 17 CLI symbols (71%) have test coverage. Uncovered symbols: `assemble`, `hooks`, `hooks_install`, `hooks_run_pre_commit`, `impact`. (Evidence: extracted test coverage data)

### NFR-CLI-05: Output Sanitization [observed]
- Generated document bodies **shall** be sanitized to remove meta-preamble, code fences, duplicate titles, and unstructured content. (Evidence: extensive `test_sanitize_generated_body_*` tests in `test_generate.py`)

### NFR-CLI-06: Section Heading Normalization [observed]
- Generated documents **shall** have section headings normalized (H3→H2 promotion, non-title H1→H2 demotion, bold pseudo-heading promotion) while preserving fenced code blocks. (Evidence: `test_normalize_section_headings_*` tests in `test_generate.py`)

## 4. Constraints

### C-CLI-01: Python / Click Framework [observed]
- The CLI **shall** be built using `click>=8.0` as the command-line framework. (Evidence: `pyproject.toml` runtime dependency, `import click` throughout `cli.py`)

### C-CLI-02: Thin Presentation Layer [observed]
- The CLI module **shall** contain no business logic — it delegates entirely to 15 application/infrastructure modules. (Evidence: every command body imports and calls a single function from another module)

### C-CLI-03: CoDD Directory Convention [observed]
- The CLI **shall** look for either `codd/` or `.codd/` as the config directory, with `--config-dir` controlling the name at init time. (Evidence: `find_codd_dir` usage at `cli.py:16-19`, `--config-dir` option at `cli.py:42-43`)

### C-CLI-04: Multi-Language Support [observed]
- The CLI **shall** support Python, TypeScript, JavaScript, and Go with full support, and Java with symbols-only support. (Evidence: `--language` help text at `cli.py:32, 428`)

### C-CLI-05: AI CLI Subprocess Model [inferred]
- AI operations **shall** be performed by invoking an external CLI command (default: `claude --print`) as a subprocess, not via direct API integration. (Evidence: `--ai-cmd` option semantics, default value mentioned in help text at `cli.py:125`)

### C-CLI-06: YAML-Based Configuration [observed]
- Project configuration **shall** be stored in `codd.yaml` with Jinja2 template rendering at init time. (Evidence: `cli.py:66`, `jinja2>=3.1.0` runtime dependency, `pyyaml>=6.0` runtime dependency)

### C-CLI-07: Wave-Sprint Execution Model [observed]
- The system **shall** organize work into waves (design document phases) and sprints (implementation phases), with integer-based numbering starting at 1. (Evidence: `IntRange(min=1)` on `--wave` and `--sprint` across commands)

## 5. Open Questions

1. **What is the intended relationship between `impact` and `propagate`?** Both analyze git diffs and map to design documents, but `impact` delegates to `propagate.run_impact` while `propagate` delegates to `propagator.run_propagate`. The naming overlap (`propagate` module vs `propagator` module) suggests possible refactoring in progress. [speculative — needs human confirmation]

2. **Why is `hooks` module classified as having 0 public / 2 internal symbols?** The CLI accesses `install_pre_commit_hook` and `run_pre_commit` directly, which are flagged as encapsulation violations. Was this an intentional design choice or an oversight? [needs review]

3. **What triggers the brownfield vs greenfield workflow?** The CLI has both `generate` (greenfield, from requirements) and `restore` (brownfield, from extracted facts), but the switching logic between these paths is not explicit in the CLI itself. [inferred — the user is expected to choose the appropriate command]

4. **Is the `.codd_version` value (`0.2.0`) intended to track the CoDD schema version or the tool version?** It is hardcoded rather than derived from `package_name="codd-dev"`. [speculative]

5. **Five CLI commands lack test coverage** (`assemble`, `hooks`, `hooks_install`, `hooks_run_pre_commit`, `impact`). Is this a known gap or are these commands considered stable? [needs review]
