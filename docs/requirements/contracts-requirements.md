---
codd:
  node_id: req:contracts
  type: requirement
  depends_on: []
  confidence: 0.65
  source: codd-require
---

# Contracts Module — Inferred Requirements

## 1. Overview

The `contracts` module provides interface contract detection for the `codd extract` pipeline. It analyzes Python modules to distinguish public API symbols from internal implementation details, and detects encapsulation violations where one module reaches into another module's internals. The module is referenced internally as requirement **R4.3**. [observed — docstring in `codd/contracts.py:1` and `tests/test_contracts.py:1`]

## 2. Functional Requirements

### FR-1: `__init__.py` Export Detection [observed]

The system shall parse `__init__.py` file content to determine which symbols a Python module publicly exports.

- **FR-1.1**: If an `__all__` list is present, it shall be treated as the authoritative set of public symbols. All other export mechanisms are ignored when `__all__` exists. [observed — `detect_init_exports` lines 49–56; `test_detect_init_exports_all_takes_priority`]
- **FR-1.2**: `__all__` parsing shall support both single-quote and double-quote string literals within the list. [observed — regex `r"""['"](\w+)['"]"""` at line 53]
- **FR-1.3**: If no `__all__` is present, the system shall fall back to detecting `from .xxx import ...` re-export statements as the public API surface. [observed — lines 58–73; `test_detect_init_exports_reexports`]
- **FR-1.4**: Re-export detection shall support `import X as Y` aliasing, treating the alias (`Y`) as the exported name. [observed — `_IMPORT_AS_RE` at line 42; test asserts `"Auth"` is exported from `AuthService as Auth`]
- **FR-1.5**: Duplicate symbol names shall be suppressed — each symbol appears at most once in the returned list. [observed — `if token not in names` / `if name not in names` guards at lines 54, 71]
- **FR-1.6**: Empty or comment-only `__init__.py` content shall produce an empty export list. [observed — `test_detect_init_exports_empty`]

### FR-2: Interface Contract Construction [observed]

The system shall build an `InterfaceContract` for every module in the extracted `ProjectFacts`.

- **FR-2.1**: For modules that contain an `__init__.py`, symbols listed in the detected exports are classified as **public**; all remaining symbols are classified as **internal**. [observed — `build_interface_contracts` lines 84–115; `test_build_interface_contracts_with_init`]
- **FR-2.2**: For single-file modules (no `__init__.py`), all symbols shall be treated as public. [observed — lines 102–104; `test_build_interface_contracts_no_init`]
- **FR-2.3**: Each contract shall compute an **API surface ratio** (`public / total`), rounded to two decimal places. [observed — line 108; tests assert `0.5` and `1.0`]
- **FR-2.4**: Modules with no symbols shall be skipped (no contract created). [observed — `if not all_symbol_names: continue` at line 91]

### FR-3: Encapsulation Violation Detection [observed]

The system shall detect cross-module encapsulation violations in a second pass after all contracts are built.

- **FR-3.1**: A violation is recorded when a module's import statements reference a symbol classified as **internal** in the target module's contract. [observed — lines 117–138; `test_encapsulation_violations`]
- **FR-3.2**: Violations shall be stored as human-readable strings in the format `"{consumer} uses {provider}.{symbol} (internal)"`. [observed — line 136]
- **FR-3.3**: Duplicate violation strings shall be suppressed. [observed — `if violation not in ... .encapsulation_violations` at line 137]

### FR-4: Data Model [observed]

The `InterfaceContract` dataclass shall capture:

| Field | Type | Description |
|-------|------|-------------|
| `module` | `str` | Module name |
| `public_symbols` | `list[str]` | Symbols considered part of the public API |
| `internal_symbols` | `list[str]` | Symbols considered internal |
| `api_surface_ratio` | `float` | Fraction of symbols that are public (0.0–1.0) |
| `encapsulation_violations` | `list[str]` | Cross-module violations detected |

[observed — `InterfaceContract` dataclass at lines 19–27]

## 3. Non-Functional Requirements

### NFR-1: Test Coverage [observed]
All three public symbols (`InterfaceContract`, `detect_init_exports`, `build_interface_contracts`) have test coverage (coverage ratio 1.0). Seven distinct test cases cover the primary behaviors including edge cases (empty input, no `__init__.py`, alias handling, `__all__` priority). [observed — `tests/test_contracts.py`; coverage metadata in extracted doc]

### NFR-2: Circular Import Avoidance [observed]
The module uses `TYPE_CHECKING` guard for the `ProjectFacts` import at module level and defers the `_language_extensions` import to function body to avoid circular imports with the `extractor` module. [observed — lines 15–16, 81]

### NFR-3: Resilient File Reading [observed]
When reading `__init__.py` files, the module uses `errors="ignore"` encoding fallback and catches all exceptions, defaulting to empty string on failure. [observed — lines 97–99]

## 4. Constraints

- **C-1**: Python-only analysis — the export detection logic (`__all__`, `from .xxx import`) is specific to Python's module system. No evidence of support for other languages' export mechanisms. [observed — regex patterns target Python syntax exclusively]
- **C-2**: Regex-based parsing — `__init__.py` analysis uses regular expressions rather than AST parsing, which limits accuracy to common formatting patterns (e.g., `__all__` must be a single bracket-delimited list). [observed — `_ALL_RE`, `_REEXPORT_FROM_RE` at lines 32–40]
- **C-3**: Depends on `ProjectFacts` and `ModuleInfo` structures from the `extractor` module — specifically `mod.files`, `mod.symbols`, `mod.internal_imports`, and `mod.interface_contract` fields. [observed — usage throughout `build_interface_contracts`]
- **C-4**: Violation detection relies on string matching (`internal_name in line`) rather than parsed import resolution, which may produce false positives if an internal symbol name appears as a substring in unrelated import text. [observed — line 135]

## 5. Open Questions

1. **Multi-line `__all__` with trailing commas or comments** — the `_ALL_RE` regex captures content between `[` and `]` with `re.DOTALL`, but does it handle all edge cases (e.g., nested brackets, f-strings)? [inferred — regex approach has inherent limitations; no tests for exotic formats]

2. **Non-Python modules** — the function imports `_language_extensions` from `extractor` but never uses it in the current implementation. Was cross-language contract detection planned? [speculative — unused import at line 81; review needed]

3. **Wildcard re-exports** — `from .models import *` is not handled by `_REEXPORT_FROM_RE`. Is this intentional (treating star-imports as non-public) or an omission? [inferred — no test or code path for `import *`]

4. **Substring false positives in violation detection** — if an internal symbol named `id` exists, any import line containing the substring `id` would match (e.g., `provider`). Is this a known limitation or a bug? [inferred — line 135 uses `if internal_name in line` rather than word-boundary matching]
