"""Tests for ``JavaScriptCompositeOracleAdapter`` (Contract Kernel oracle dispatch
— the JAVASCRIPT SWITCH, closing the §9 UNSUPPORTED_EXPLICIT RED javascript.yaml
used to leave open by declaring no ``implement_oracle`` at all).

Two layers of coverage, mirroring ``tests/languages/test_oracle_python_parity.py``
(the closest precedent — Python is JS's structural sibling: no compiler, an
in-process ``kind=adapter`` composite):

1. Registration — the adapter resolves generically through the SAME registry/
   contract dispatch every other built-in oracle uses (no ``if language ==
   "javascript"`` anywhere).
2. Gate-level behaviour — drives the REAL entry point
   (:func:`codd.implement_oracle.run_implement_oracle_gate` with
   ``language="javascript"``) over small scaffolded fixture projects, proving:
   a coherent project passes; a syntax error, a broken relative import, and a
   missing named export are each caught and categorized; third-party/bare
   imports are tolerated (never a false-RED); a commented-out bad import is
   never scanned; and an empty project is an honest ``OracleScopeError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.implement_oracle import (
    EVIDENCE_ENVIRONMENT_BUILD,
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    EVIDENCE_OTHER,
    OracleScopeError,
    run_implement_oracle_gate,
)
from codd.languages.builtin_adapters import ensure_builtin_adapters_registered
from codd.languages.contract import KIND_IMPLEMENT_ORACLE
from codd.languages.registry import AdapterRegistry, default_adapter_registry


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _scaffold_coherent(root: Path) -> None:
    """A minimal, fully-coherent plain-JS project (ESM, ``src/`` + ``tests/``)."""
    _write(root, "package.json", '{"name": "app", "version": "0.1.0", "type": "module"}\n')
    _write(
        root,
        "src/math.js",
        "export function add(a, b) {\n  return a + b;\n}\n\nexport const PI = 3.14159;\n",
    )
    _write(
        root,
        "src/index.js",
        "export { add, PI } from './math.js';\n",
    )
    _write(
        root,
        "tests/math.test.js",
        (
            "import { add } from '../src/math.js';\n"
            "import { describe, it } from 'node:test';\n"
            "import assert from 'node:assert/strict';\n\n"
            "describe('add', () => {\n"
            "  it('adds', () => { assert.strictEqual(add(2, 3), 5); });\n"
            "});\n"
        ),
    )


def _run(root: Path, config: dict | None = None):
    return run_implement_oracle_gate(
        root,
        language="javascript",
        project_name="app",
        config=config or {},
        echo=lambda _m: None,
    )


def _codes(result) -> set[str]:
    return {f.code for f in result.findings}


def _categories(result) -> set[str]:
    return {f.category for f in result.findings}


# ════════════════════════════════════════════════════════════════════════════
# 0. Registration — generic dispatch, no language-name branch.
# ════════════════════════════════════════════════════════════════════════════


def test_javascript_composite_is_registered_under_implement_oracle_kind() -> None:
    ensure_builtin_adapters_registered(default_adapter_registry)
    adapter = default_adapter_registry.get(KIND_IMPLEMENT_ORACLE, "javascript-composite")
    assert adapter is not None
    assert type(adapter).__name__ == "JavaScriptCompositeOracleAdapter"


def test_registering_javascript_composite_twice_is_idempotent() -> None:
    """Re-registering the SAME adapter type on a fresh registry is a no-op, not
    a collision (mirrors every other builtin adapter's fail-closed contract)."""
    registry = AdapterRegistry()
    ensure_builtin_adapters_registered(registry)
    ensure_builtin_adapters_registered(registry)  # must not raise
    assert registry.get(KIND_IMPLEMENT_ORACLE, "javascript-composite") is not None


# ════════════════════════════════════════════════════════════════════════════
# 1. A fully-coherent project → passed=True, executed=True, no findings.
# ════════════════════════════════════════════════════════════════════════════


def test_coherent_project_passes(tmp_path: Path) -> None:
    _scaffold_coherent(tmp_path)
    result = _run(tmp_path)
    assert result.executed is True
    assert result.passed is True, result.findings
    assert result.findings == []


# ════════════════════════════════════════════════════════════════════════════
# 2. Empty project → OracleScopeError (anti-false-green: never a green empty scope).
# ════════════════════════════════════════════════════════════════════════════


def test_empty_project_raises_oracle_scope_error(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"name": "app"}\n')
    with pytest.raises(OracleScopeError):
        _run(tmp_path)


# ════════════════════════════════════════════════════════════════════════════
# 3. Layer 1 — syntax errors are caught; ``.jsx`` is never syntax-checked.
# ════════════════════════════════════════════════════════════════════════════


def test_syntax_error_in_source_is_red(tmp_path: Path) -> None:
    _scaffold_coherent(tmp_path)
    _write(tmp_path, "src/broken.js", "export function broken( {\n")
    result = _run(tmp_path)
    assert result.passed is False
    assert "JS_SYNTAX_ERROR" in _codes(result)
    assert EVIDENCE_OTHER in _categories(result)
    assert any(f.path == "src/broken.js" for f in result.findings)


def test_syntax_error_in_test_file_is_red(tmp_path: Path) -> None:
    _scaffold_coherent(tmp_path)
    _write(tmp_path, "tests/broken.test.js", "const x = ;\n")
    result = _run(tmp_path)
    assert result.passed is False
    assert "JS_SYNTAX_ERROR" in _codes(result)


def test_jsx_file_is_never_syntax_checked(tmp_path: Path) -> None:
    """A syntactically-valid-for-JSX-but-invalid-for-plain-node .jsx file must
    NOT false-RED — plain ``node --check`` has no JSX transform, so layer 1
    deliberately excludes .jsx (see the adapter's module docstring)."""
    _scaffold_coherent(tmp_path)
    _write(tmp_path, "src/widget.jsx", "export function Widget() {\n  return <div>hi</div>;\n}\n")
    result = _run(tmp_path)
    assert result.passed is True, result.findings


# ════════════════════════════════════════════════════════════════════════════
# 4. Layer 2 — the keystone: first-party import/export resolution.
# ════════════════════════════════════════════════════════════════════════════


def test_import_from_nonexistent_file_is_red_module_resolution(tmp_path: Path) -> None:
    _scaffold_coherent(tmp_path)
    _write(
        tmp_path,
        "tests/broken_import.test.js",
        "import { add } from '../src/does_not_exist.js';\n",
    )
    result = _run(tmp_path)
    assert result.passed is False
    assert "JS_MODULE_NOT_FOUND" in _codes(result)
    assert EVIDENCE_MODULE_RESOLUTION in _categories(result)


def test_import_of_missing_named_export_is_red_missing_symbol(tmp_path: Path) -> None:
    """The TS oracle's own motivating bug class: a test imports a name the
    target module does not actually export (module exists, symbol does not)."""
    _scaffold_coherent(tmp_path)
    _write(
        tmp_path,
        "tests/typo_import.test.js",
        "import { addTypo } from '../src/math.js';\n",
    )
    result = _run(tmp_path)
    assert result.passed is False
    assert "JS_IMPORT_NAME_NOT_FOUND" in _codes(result)
    assert EVIDENCE_MISSING_SYMBOL in _categories(result)


def test_correct_named_import_from_barrel_reexport_passes(tmp_path: Path) -> None:
    """``src/index.js`` re-exports ``add``/``PI`` from ``./math.js`` (a common
    barrel pattern, e.g. this codebase's own dogfood ExprCalc ``src/index.js``).
    An importer of the barrel gets the transitively-resolved names."""
    _scaffold_coherent(tmp_path)
    _write(
        tmp_path,
        "tests/via_barrel.test.js",
        "import { add, PI } from '../src/index.js';\n",
    )
    result = _run(tmp_path)
    assert result.passed is True, result.findings


def test_third_party_bare_import_is_tolerated(tmp_path: Path) -> None:
    """A bare specifier (npm package) is never first-party — never checked, even
    though it is not installed (no ``node_modules``) — anti-false-RED."""
    _scaffold_coherent(tmp_path)
    _write(
        tmp_path,
        "tests/uses_lodash.test.js",
        "import { isEqual } from 'lodash';\nimport { readFile } from 'node:fs/promises';\n",
    )
    result = _run(tmp_path)
    assert result.passed is True, result.findings


def test_default_import_is_resolution_only_never_symbol_checked(tmp_path: Path) -> None:
    """A default import is checked for FILE resolution only — CommonJS/ESM
    default-export interop has too many legitimate shapes to honestly claim a
    missing default without a real module loader (see module docstring)."""
    _scaffold_coherent(tmp_path)
    _write(tmp_path, "src/plain.js", "export const value = 1;\n")  # no default export at all
    _write(tmp_path, "tests/default_import.test.js", "import whatever from '../src/plain.js';\n")
    result = _run(tmp_path)
    assert result.passed is True, result.findings


def test_broken_default_import_target_is_still_module_resolution_checked(tmp_path: Path) -> None:
    _scaffold_coherent(tmp_path)
    _write(tmp_path, "tests/broken_default.test.js", "import whatever from '../src/nope.js';\n")
    result = _run(tmp_path)
    assert result.passed is False
    assert "JS_MODULE_NOT_FOUND" in _codes(result)


def test_commented_out_bad_import_is_never_scanned(tmp_path: Path) -> None:
    _scaffold_coherent(tmp_path)
    _write(
        tmp_path,
        "tests/has_comment.test.js",
        "// import { nope } from '../src/does_not_exist.js';\n"
        "/* also import { nope2 } from '../src/also_missing.js'; */\n"
        "import { add } from '../src/math.js';\n",
    )
    result = _run(tmp_path)
    assert result.passed is True, result.findings


def test_require_of_nonexistent_file_is_red(tmp_path: Path) -> None:
    """CJS ``require()`` is resolution-checked exactly like an ESM import."""
    _scaffold_coherent(tmp_path)
    _write(tmp_path, "tests/cjs_style.test.js", "const missing = require('../src/nope.cjs');\n")
    result = _run(tmp_path)
    assert result.passed is False
    assert "JS_MODULE_NOT_FOUND" in _codes(result)


# ════════════════════════════════════════════════════════════════════════════
# 5. Environment failures are honest, never a silent pass.
# ════════════════════════════════════════════════════════════════════════════


def test_node_missing_from_path_is_environment_build_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scaffold_coherent(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))  # a PATH with no `node` binary on it
    result = _run(tmp_path)
    assert result.passed is False
    assert EVIDENCE_ENVIRONMENT_BUILD in _categories(result)
