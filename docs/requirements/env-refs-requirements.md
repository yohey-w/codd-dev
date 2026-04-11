---
codd:
  node_id: req:env-refs
  type: requirement
  depends_on: []
  confidence: 0.65
  source: codd-require
---

# ENV Refs Requirements

Now I have the full source code and tests. Here is the inferred requirements document:

# Environment Variable & Config Dependency Detection (env-refs)

## 1. Overview

The `env_refs` module provides static detection of environment variable and configuration key references in source code. It is part of the `codd extract` pipeline (referenced as "R8" in source docstrings — `codd/env_refs.py:1`) and surfaces runtime configuration dependencies that are invisible in import or call graphs. The module scans source file content using regex-based pattern matching and attaches discovered references to module-level project facts for downstream consumption by the extractor. [observed]

## 2. Functional Requirements

### FR-1: Environment Variable Detection — Python [observed]

The system shall detect references to environment variables accessed via the Python `os` module in the following forms:

| Pattern | Default Detection | Evidence |
|---------|-------------------|----------|
| `os.getenv("KEY")` / `os.getenv("KEY", default)` | Yes — second-arg heuristic | `codd/env_refs.py:30-33`, `_PY_GETENV_RE` |
| `os.environ["KEY"]` | No — always `has_default=False` | `codd/env_refs.py:36-37`, `_PY_ENVIRON_BRACKET_RE` |
| `os.environ.get("KEY")` / `os.environ.get("KEY", default)` | Yes — second-arg heuristic | `codd/env_refs.py:41-42`, `_PY_ENVIRON_GET_RE` |
| `os.environ.pop("KEY")` / `os.environ.pop("KEY", default)` | Yes — second-arg heuristic | `codd/env_refs.py:46-47`, `_PY_ENVIRON_POP_RE` |

Each detected reference shall be classified with `kind="env"`. [observed]

### FR-2: Environment Variable Detection — TypeScript/JavaScript [observed]

The system shall detect references to environment variables accessed via `process.env` in two forms:

- **Dot notation** (`process.env.KEY`): Only matches identifiers beginning with an uppercase letter followed by uppercase letters, digits, or underscores (`[A-Z_][A-Z0-9_]*`). Lowercase identifiers are intentionally excluded to reduce false positives. (`codd/env_refs.py:53-54`, `tests/test_env_refs.py:86-89`) [observed]
- **Bracket notation** (`process.env["KEY"]`): Matches any quoted string key, regardless of case. (`codd/env_refs.py:58-59`, `tests/test_env_refs.py:93-96`) [observed]

JS/TS environment references are always reported with `has_default=False`. [observed]

### FR-3: Configuration Key Detection — Python [observed]

The system shall detect references to configuration keys via:

- **Bracket access** on known config object names: `config`, `settings`, `cfg`, `conf`, `app.config`, `current_app.config` — matching `["KEY"]` access. (`codd/env_refs.py:65-66`, `_PY_CONFIG_BRACKET_RE`) [observed]
- **Attribute access** on `settings`: Only matches `UPPER_CASE` attribute names (`settings.EMAIL_BACKEND`). (`codd/env_refs.py:70-71`, `_PY_SETTINGS_ATTR_RE`) [observed]

All config references are classified with `kind="config"` and `has_default=False`. [observed]

### FR-4: EnvRef Data Model [observed]

Each detected reference shall be represented as an `EnvRef` dataclass (`codd/env_refs.py:18-25`) with the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `key` | `str` | The environment variable or config key name |
| `kind` | `str` | `"env"` or `"config"` |
| `file` | `str` | Relative file path where the reference was found |
| `line` | `int` | 1-based line number |
| `has_default` | `bool` | Whether a default/fallback value is provided |

### FR-5: Default Value Detection Heuristic [observed]

For call-style patterns (`os.getenv`, `os.environ.get`, `os.environ.pop`), the system uses a shallow parenthesis-tracking scan (`_has_second_arg`, `codd/env_refs.py:75-99`) to detect whether a comma exists after the first argument and before the matching close-paren. This determines the `has_default` flag. The scan handles nested brackets/parens at arbitrary depth. [observed]

### FR-6: Multiple References Per Line [observed]

The system shall detect multiple environment/config references on a single source line. All regex patterns use `finditer` for multi-match scanning. (`tests/test_env_refs.py:55-59`) [observed]

### FR-7: Line Number Tracking [observed]

Each `EnvRef` shall record the 1-based line number where the reference occurs. (`tests/test_env_refs.py:61-66`) [observed]

### FR-8: Module-Level Aggregation via `build_env_refs` [observed]

The `build_env_refs` function (`codd/env_refs.py:192-204`) shall iterate over all modules in `ProjectFacts`, read each module's source files from disk, run `detect_env_refs` on the content, and assign the collected `EnvRef` list to `mod.env_refs`. [observed]

### FR-9: Graceful Handling of Missing/Unreadable Files [observed]

When a module's source file does not exist or cannot be read, `build_env_refs` shall silently skip it and continue processing remaining files. The module's `env_refs` will be an empty list. (`codd/env_refs.py:198-201`, `tests/test_env_refs.py:180-197`) [observed]

## 3. Non-Functional Requirements

### NFR-1: Regex-Based Static Analysis [observed]

Detection is implemented entirely via compiled regular expressions applied line-by-line — no AST parsing is used. This makes the module language-agnostic at the pattern level and fast, but inherently heuristic. (`codd/env_refs.py:28-71`) [observed]

### NFR-2: Known False Positive Tolerance [observed]

The system intentionally matches environment references inside comments. The test suite explicitly documents this as an acceptable heuristic trade-off: `# This actually matches (regex is line-based), which is fine` (`tests/test_env_refs.py:144-145`). [observed]

### NFR-3: Full Test Coverage [observed]

All 3 public symbols (`EnvRef`, `detect_env_refs`, `build_env_refs`) have corresponding test coverage. The extracted metadata reports coverage ratio 1.0 (3/3). Tests cover Python env patterns, JS/TS patterns, config patterns, false positive scenarios, integration with `build_env_refs`, and the missing-file edge case. [observed]

### NFR-4: Encoding Tolerance [observed]

File reading uses `errors="ignore"` to handle files with encoding issues without raising exceptions. (`codd/env_refs.py:199`) [observed]

## 4. Constraints

### C-1: Infrastructure Layer Module [observed]

`env_refs` is classified in the Infrastructure layer of the architecture. It has a single dependency on the `extractor` module (for the `ProjectFacts` type), used only as a `TYPE_CHECKING` import to avoid circular dependencies. (`codd/env_refs.py:14-15`) [observed]

### C-2: Pure Python, No External Dependencies [observed]

The module uses only Python standard library (`re`, `dataclasses`, `pathlib`, `typing`). No third-party packages are required. [observed]

### C-3: Synchronous Execution [observed]

All functions are synchronous. No async patterns are used. [observed]

### C-4: Line-Based Processing [observed]

Detection operates on individual lines (`content.splitlines()`), not on multi-line constructs. Environment references spanning multiple lines will not be detected. (`codd/env_refs.py:105-106`) [inferred]

### C-5: Single-Quote and Double-Quote Support [observed]

All regex patterns accept both single-quoted (`'KEY'`) and double-quoted (`"KEY"`) string literals. (`codd/env_refs.py:31-32, 37, 42, 47, 59, 66`) [observed]

## 5. Open Questions

1. **Comment filtering**: The module matches patterns inside comments (documented as acceptable). Should a future version strip comments before scanning to reduce noise, or is the current heuristic sufficient? [speculative — needs human confirmation]

2. **Multi-line pattern support**: Calls like `os.getenv(\n    "KEY"\n)` split across lines will not be detected. Is this a known limitation accepted by design? [inferred — no multi-line handling observed]

3. **Additional language support**: Only Python and JS/TS patterns are implemented. Other languages (Go `os.Getenv`, Ruby `ENV["KEY"]`, etc.) are absent. Is cross-language expansion planned? [speculative]

4. **Config key naming conventions**: `settings.ATTR` only matches `UPPER_CASE` attributes. Django-style lowercase settings (if any exist in target codebases) would be missed. Is this intentional? [inferred from `_PY_SETTINGS_ATTR_RE` restriction at `codd/env_refs.py:70-71`]

5. **`has_default` for config patterns**: All config references (`kind="config"`) report `has_default=False` regardless of actual usage. Was default-detection intentionally omitted for config patterns, or is it a gap? [inferred — bracket access like `config.get("KEY", default)` is not handled]
