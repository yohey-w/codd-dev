---
codd:
  node_id: req:assembler
  type: requirement
  depends_on: []
  confidence: 0.65
  source: codd-require
---

# Assembler Module — Inferred Requirements

## 1. Overview

The assembler module integrates AI-generated code fragments (produced by the `implement` command in earlier sprint stages) into a single, buildable project. It reads design documents and generated code fragments, constructs an AI prompt requesting integration, invokes an external AI command, and writes the resulting files to disk. It sits at the Application layer, called directly by the CLI (`cli -> assembler`), and depends on the `generator` and `scanner` infrastructure modules for configuration loading, AI command resolution/invocation, and document scanning. [observed]

## 2. Functional Requirements

### FR-1: Load project configuration [observed]
The assembler must load the project configuration via `_load_project_config(project_root)` from the `generator` module, using the resolved project root path. This configuration drives language detection, project naming, AI command selection, source directory discovery, and convention normalization.
- **Evidence**: `codd/assembler.py:31` — `config = _load_project_config(project_root)`

### FR-2: Resolve the AI command [observed]
The assembler must resolve which AI command to invoke, using the project config, an optional user-supplied `ai_command` override, and the command name `"assemble"`.
- **Evidence**: `codd/assembler.py:32` — `generator_module._resolve_ai_command(config, ai_command, command_name="assemble")`

### FR-3: Collect design documents [observed]
The assembler must scan the project for all design documents using `build_document_node_path_map`, read each file, strip YAML frontmatter, and pass the content (with `node_id` and relative path) to the prompt.
- **Evidence**: `codd/assembler.py:63-78` — `_collect_design_documents`; imports `build_document_node_path_map` from `codd.scanner` and `_extract_frontmatter` (though `_strip_frontmatter` is used locally).

### FR-4: Collect generated code fragments [observed]
The assembler must locate generated code fragments under `<source_dir>/generated/sprint_N/` directories. It searches configured `scan.source_dirs` (defaulting to `["src/"]`) for a `generated/` subdirectory, then collects files matching a set of supported extensions.
- **Supported extensions**: `.ts`, `.tsx`, `.js`, `.jsx`, `.py`, `.go`, `.java`, `.css` [observed]
- **Sprint ordering**: Directories must be named `sprint_*` and are sorted lexicographically. [observed]
- **Evidence**: `codd/assembler.py:81-111`

### FR-5: Require fragments exist [observed]
If no generated fragments are found, the assembler must raise a `ValueError` directing the user to run `codd implement` first.
- **Evidence**: `codd/assembler.py:40-41`

### FR-6: Build an AI assembly prompt [observed]
The assembler must construct a structured prompt that:
1. Instructs the AI to act as a "code assembler" for the project's language.
2. Includes the project name and language from config.
3. Provides numbered instructions covering: reading design docs, reading fragments, producing a complete buildable project (config files, entry points, source code, styles), resolving cross-sprint conflicts (later sprints override earlier), ensuring import correctness, not adding unspecified features, and preserving traceability comments (`@generated-by`, `@generated-from`).
4. Specifies a strict output format: `=== FILE: path/to/file ===` blocks with file contents.
5. Appends all design documents grouped by node_id.
6. Appends all generated fragments grouped by sprint directory.
- **Evidence**: `codd/assembler.py:125-189`

### FR-7: Invoke external AI command [observed]
The assembler must invoke the resolved AI command with the assembled prompt via `generator_module._invoke_ai_command` and capture the raw text output.
- **Evidence**: `codd/assembler.py:51`

### FR-8: Parse and write assembled files [observed]
The assembler must parse the AI's raw output for `=== FILE: <path> ===` delimited blocks, extract file content between delimiters, strip optional code fences (` ``` `) from the beginning and end of each block, and write each file relative to the project root. Parent directories must be created automatically (`mkdir(parents=True, exist_ok=True)`). A trailing newline is appended to each file.
- **Evidence**: `codd/assembler.py:192-225`
- If the AI output contains zero `=== FILE: ... ===` blocks, a `ValueError` must be raised. [observed]

### FR-9: Return an AssembleResult [observed]
The function must return an `AssembleResult` dataclass containing: the resolved output directory path, the count of files written, and the raw AI output text.
- **Evidence**: `codd/assembler.py:14-20`, `codd/assembler.py:56-60`

### FR-10: Configurable output directory [observed]
The output directory defaults to `"src"` but can be overridden via the `output_dir` parameter. The destination path is resolved relative to the project root.
- **Evidence**: `codd/assembler.py:44-45`

## 3. Non-Functional Requirements

### NFR-1: Synchronous execution [observed]
The assembler operates entirely synchronously — no async functions or concurrency constructs are used. AI invocation is a blocking call.
- **Evidence**: `codd/assembler.py` — no `async` keywords; module map shows 0 async functions.

### NFR-2: Immutable result type [observed]
`AssembleResult` is a frozen dataclass, ensuring results cannot be mutated after creation.
- **Evidence**: `codd/assembler.py:14` — `@dataclass(frozen=True)`

### NFR-3: No test coverage [observed]
The module has 0% test coverage (0 of 2 public symbols covered). [observed]
- **Evidence**: Extracted module metadata — `Coverage: 0.0 (0 / 2)`.

### NFR-4: Moderate change risk [observed]
The module has a change risk score of 0.53, driven primarily by zero test coverage and a small dependent surface (only CLI depends on it).
- **Evidence**: Architecture overview risk table — `assembler: 0.53`.

## 4. Constraints

### C-1: Dependency on generator internals [observed]
The assembler imports private functions (`_load_project_config`, `_normalize_conventions`, `_resolve_ai_command`, `_invoke_ai_command`) from `codd.generator`. This creates a tight coupling to the generator module's internal implementation.
- **Evidence**: `codd/assembler.py:9-10`

### C-2: Dependency on scanner internals [observed]
The assembler imports `_extract_frontmatter` and `build_document_node_path_map` from `codd.scanner`.
- **Evidence**: `codd/assembler.py:11`
- Note: `_extract_frontmatter` is imported but not directly used; `_strip_frontmatter` is a local re-implementation. [inferred]

### C-3: Multi-language fragment support [observed]
The fragment collector supports files in TypeScript, JavaScript, Python, Go, Java, and CSS. The prompt language is configured via `project.language` in config (defaulting to `"typescript"`).
- **Evidence**: `codd/assembler.py:102`, `codd/assembler.py:132`

### C-4: AI output format contract [observed]
The assembler depends on the AI producing output in a specific `=== FILE: path ===` delimited format. Any deviation (no blocks emitted) results in a hard failure.
- **Evidence**: `codd/assembler.py:196-203`

### C-5: File writes are unconditional [observed]
The assembler overwrites existing files without confirmation, backup, or diff. All file paths from AI output are written directly relative to the project root, including paths outside the designated output directory (e.g., `package.json` at root).
- **Evidence**: `codd/assembler.py:220-222` — `out_path = project_root / file_path_str` with no path validation.

### C-6: UTF-8 encoding [observed]
All file reads and writes use UTF-8 encoding explicitly.
- **Evidence**: `codd/assembler.py:70`, `codd/assembler.py:104`, `codd/assembler.py:222`

## 5. Open Questions

1. **Unused import**: `_extract_frontmatter` is imported from `codd.scanner` but `_strip_frontmatter` is implemented locally with similar functionality. Is this an oversight, or does the local version intentionally differ? [speculative — review needed]

2. **Unused import**: `_normalize_conventions` is imported from `codd.generator` but never called in the current code. Was this planned for use in prompt construction? [speculative — review needed]

3. **Path traversal risk**: The assembler writes files to any path the AI specifies relative to `project_root`, with no allowlist or sandboxing. Could a malicious or hallucinated AI output write files outside the intended output directory? [inferred — review needed]

4. **Sprint conflict resolution**: The prompt instructs the AI to resolve conflicts where "later sprints may refine or replace earlier ones," but the assembler provides no structural guidance (e.g., ordering metadata). Is lexicographic sort of `sprint_N` directories sufficient for all sprint numbering schemes? [inferred]

5. **Missing file extensions**: The fragment collector excludes some common source file types (e.g., `.html`, `.json`, `.yaml`, `.sql`, `.rs`, `.kt`). Is this intentional scoping or an omission? [speculative — review needed]
