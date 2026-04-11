---
codd:
  node_id: req:config
  type: requirement
  depends_on: []
  confidence: 1.0
  source: codd-require
---

# Config Requirements

Now I have all the concrete details. Here's the inferred requirements document:

# Config Module — Inferred Requirements

## 1. Overview

The `config` module is an infrastructure-layer component responsible for discovering the CoDD configuration directory within a project and loading a merged configuration from built-in defaults and project-level overrides. It serves as a foundational dependency consumed by at least 10 other modules (`cli`, `extractor`, `generator`, `hooks`, `planner`, `propagator`, `require`, `restore`, `reviewer`, `verifier`) per the dependency graph. [observed]

## 2. Functional Requirements

### FR-CFG-1: Configuration Directory Discovery [observed]

The system shall locate a CoDD configuration directory under a given project root by checking for directories named `codd/` and `.codd/` in that priority order. A directory qualifies only if it contains a `codd.yaml` file. If neither candidate qualifies, the function returns `None`.

- **Evidence**: `find_codd_dir()` at `codd/config.py:19-30`; `CODD_DIR_CANDIDATES = ("codd", ".codd")` at line 16.

### FR-CFG-2: Configuration Loading with Defaults Merge [observed]

The system shall load configuration by:
1. Discovering the CoDD config directory (FR-CFG-1).
2. Reading a bundled `defaults.yaml` file shipped alongside the module.
3. Reading the project's `codd.yaml` from the discovered directory.
4. Deep-merging the project config on top of defaults, producing a single merged dict.

If no config directory is found, a `FileNotFoundError` is raised with a message listing the candidate directory names checked.

- **Evidence**: `load_project_config()` at `codd/config.py:33-47`; `DEFAULTS_PATH` at line 13.

### FR-CFG-3: Deep Merge Semantics [observed]

The merge algorithm shall apply these rules recursively:
- **Dicts**: project keys override default keys; keys present only in defaults are preserved. Merge is recursive for nested dicts.
- **Lists**: lists are union-merged with deduplication. Items from both defaults and project are combined; duplicates (determined by JSON serialization with sorted keys) are removed while preserving insertion order (defaults first, then project).
- **Scalars**: project values replace default values entirely.

- **Evidence**: `_deep_merge()` at `codd/config.py:60-73`; `_merge_lists()` at `codd/config.py:76-85`; test assertions at `tests/test_config.py:63-76` confirm scalar override (`coding_principles`), list union (`doc_dirs`, `exclude`, `conventions`), and default preservation (`ai_command`).

### FR-CFG-4: YAML-Only Configuration Format [observed]

Configuration files must be valid YAML mappings (top-level dict). If a file parses to a non-dict type, a `ValueError` is raised. Files are read with UTF-8 encoding. The `yaml.safe_load` parser is used (no arbitrary Python object deserialization).

- **Evidence**: `_read_yaml_mapping()` at `codd/config.py:50-57`.

### FR-CFG-5: Default Configuration Schema [observed]

The bundled defaults define the following top-level keys and their default values:
- `version` — config schema version (currently `"0.2.0a1"`)
- `project.frameworks` — empty list
- `ai_command` — a Claude CLI invocation string
- `coding_principles` — `null`
- `scan.source_dirs`, `scan.test_dirs`, `scan.doc_dirs`, `scan.config_files`, `scan.exclude` — directory/file glob lists with sensible defaults
- `graph.store`, `graph.path` — graph storage configuration
- `bands.green`, `bands.amber` — confidence band thresholds with `min_confidence` and `min_evidence_count`
- `propagation.max_depth`, `propagation.stop_at_contract_boundary` — propagation behavior settings
- `conventions` — empty list

- **Evidence**: `codd/defaults.yaml` (full file, 31 lines).

## 3. Non-Functional Requirements

### NFR-CFG-1: Immutability of Inputs [observed]

The merge operation uses `deepcopy` on all values to ensure the original default and project dicts are not mutated during or after merging.

- **Evidence**: `deepcopy` calls at `codd/config.py:6,62,67,73,84`.

### NFR-CFG-2: Deterministic List Deduplication [observed]

List deduplication uses JSON serialization with `sort_keys=True` and `ensure_ascii=False` as a canonical representation for equality comparison, ensuring deterministic and Unicode-safe deduplication regardless of dict key order within list items.

- **Evidence**: `_merge_lists()` at `codd/config.py:80-81`.

### NFR-CFG-3: Test Coverage [observed]

The module has 50% symbol coverage (1 of 2 public functions tested). `load_project_config` is tested; `find_codd_dir` is not directly tested. [inferred] The merge test exercises scalar override, nested dict merge, and list union semantics in a single test case.

- **Evidence**: `tests/test_config.py`; coverage metadata in extracted module doc.

### NFR-CFG-4: Change Risk [observed]

The module has a change-risk score of 0.60 — the second highest in the system — driven primarily by a high dependent count (0.83 normalized, ~10 direct consumers) combined with partial test coverage.

- **Evidence**: Architecture overview change risk table.

## 4. Constraints

### C-CFG-1: YAML Parser Dependency [observed]

The module depends on PyYAML (`yaml` / `pyyaml>=6.0`) as its sole external parsing dependency. Configuration files must be YAML; no JSON, TOML, or other format support is provided.

- **Evidence**: `import yaml` at `codd/config.py`; `pyyaml>=6.0` in `pyproject.toml` runtime dependencies.

### C-CFG-2: Standard Library Only Beyond YAML [observed]

Apart from PyYAML, the module uses only standard library packages (`copy`, `json`, `pathlib`, `typing`). No additional runtime dependencies are introduced.

- **Evidence**: Import statements at `codd/config.py:3-10`.

### C-CFG-3: File-System Convention [observed]

The config directory must be a direct child of the project root named either `codd/` or `.codd/`, and must contain a file named exactly `codd.yaml`. This naming convention is hardcoded, not configurable.

- **Evidence**: `CODD_DIR_CANDIDATES` constant and `find_codd_dir` logic at `codd/config.py:16,26-29`.

### C-CFG-4: Bundled Defaults Co-Located with Module [observed]

The defaults file must be located alongside `config.py` in the same package directory (resolved via `Path(__file__).with_name("defaults.yaml")`). This couples the defaults file to the package installation layout.

- **Evidence**: `DEFAULTS_PATH` at `codd/config.py:13`.

## 5. Open Questions

1. **Why is `find_codd_dir` untested?** Given its role as the entry point for directory discovery and its impact on all downstream consumers, the lack of direct test coverage is notable. Is this an intentional deferral or an oversight? [review-needed]

2. **Is the `codd/` vs `.codd/` priority order documented for end users?** The code silently prefers `codd/` over `.codd/`. If a project has both directories, only `codd/` is used. It is unclear whether this precedence is communicated to users. [speculative]

3. **List deduplication via JSON serialization**: The deduplication strategy may produce unexpected results for list items containing floats (JSON representation differences) or complex nested structures. Is this edge case acceptable? [speculative]

4. **Config schema versioning**: `defaults.yaml` declares `version: "0.2.0a1"` but no code validates or acts on this version field. Is version-based migration or compatibility checking planned? [speculative, review-needed]

5. **`json` import appears in source but not in the extracted dependency list**: The extracted module doc lists only `copy` and `yaml` as external dependencies, but the actual source also imports `json` (stdlib). This is a minor extraction accuracy issue. [observed]
