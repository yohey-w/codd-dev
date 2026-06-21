"""Python composite implement-oracle PARITY characterization (Contract Kernel oracle
dispatch, step 6 — the PYTHON SWITCH).

This file PINS the observable behaviour of the live Python implement-oracle at the
gate boundary (:func:`codd.implement_oracle.run_implement_oracle_gate` with
``language="python"``) so the step-6 switch — moving Python off the hand-written
``_run_python_composite_oracle`` (in-process compile + first-party import resolver +
``pytest --collect-only``) and onto the Contract-Kernel contract path (the
``kind="adapter"`` ``python-composite`` :class:`ImplementOracleExecutorAdapter`
whose ``execute(ctx)`` runs the SAME three layers) — is proven behaviour-preserving
at the CATEGORY level (GPT §7 parity note: ``findings.category`` / ``code`` /
``path``, ``passed``, ``executed``, ``failed_paths`` must match; NOT byte-for-byte).

The cardinal rule is anti-false-green: a non-coherent Python module must NEVER
pass. Python is the language CoDD itself is written in, so this oracle is dogfooded
constantly — a regression breaks greenfield/implement. These fixtures are the
category-level oracle the switch must reproduce.

(The richer behavioural fixtures — TYPE_CHECKING / guarded-import / PEP-562
``__getattr__`` / re-export tolerances — live in
``tests/test_python_implement_oracle.py`` and ALSO drive the same gate boundary, so
they characterize the switch too. This file adds the explicit
passed/executed/category/code/failed_paths assertions the parity note calls for.)
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

_PACKAGE = "app"


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _scaffold(root: Path, *, with_path_shim: bool = True) -> None:
    """A minimal src-layout Python project (package ``app`` under ``src``).

    ``with_path_shim`` installs a root ``conftest.py`` that puts ``src`` on
    ``sys.path`` so ``--collect-only`` resolves the package WITHOUT an editable
    install (the deterministic, dependency-free equivalent of the greenfield flow).
    """
    _write(root, "src/app/__init__.py", "")
    _write(root, "tests/__init__.py", "")
    if with_path_shim:
        _write(
            root,
            "conftest.py",
            "import os, sys\n"
            "sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))\n",
        )


def _run(root: Path, config: dict | None = None):
    return run_implement_oracle_gate(
        root,
        language="python",
        project_name=_PACKAGE,
        config=config or {},
        echo=lambda _m: None,
    )


def _codes(result) -> set[str]:
    return {f.code for f in result.findings}


def _categories(result) -> set[str]:
    return {f.category for f in result.findings}


# ════════════════════════════════════════════════════════════════════════════
# 1. A fully-coherent package → passed=True, executed=True, no findings.
# ════════════════════════════════════════════════════════════════════════════


def test_parity_clean_package_passes(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write(tmp_path, "src/app/helpers.py", "def add(a, b):\n    return a + b\n")
    _write(
        tmp_path,
        "src/app/core.py",
        "from .helpers import add\n\n\ndef total(xs):\n    acc = 0\n"
        "    for x in xs:\n        acc = add(acc, x)\n    return acc\n",
    )
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import total\n\n\ndef test_total():\n    assert total([1, 2, 3]) == 6\n",
    )

    result = _run(tmp_path)

    assert result.passed is True, [(f.category, f.code, f.message) for f in result.findings]
    assert result.executed is True
    assert result.findings == []


# ════════════════════════════════════════════════════════════════════════════
# 2. A syntax/compile error in source → RED via the compile layer (type_error).
# ════════════════════════════════════════════════════════════════════════════


def test_parity_compile_error_is_red(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write(tmp_path, "src/app/broken.py", "def f(:\n    return 1\n")  # invalid syntax
    _write(tmp_path, "src/app/core.py", "def ok():\n    return 1\n")
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import ok\n\n\ndef test_ok():\n    assert ok() == 1\n",
    )

    result = _run(tmp_path)

    assert result.passed is False
    assert result.executed is True
    # compile layer classifies a SyntaxError as the coherence category (type_error).
    assert EVIDENCE_OTHER in _categories(result)
    assert any(c in ("SyntaxError", "IndentationError", "TabError") for c in _codes(result)), _codes(result)
    assert any(p and p.endswith("broken.py") for p in result.failed_paths), result.failed_paths


# ════════════════════════════════════════════════════════════════════════════
# 3. A first-party module that is provably absent → RED (module_resolution).
#    THE keystone: unimported by any test → only the static resolver proves it.
# ════════════════════════════════════════════════════════════════════════════


def test_parity_first_party_missing_module_is_red(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write(tmp_path, "src/app/hidden.py", "from .missing import X\n")
    _write(tmp_path, "src/app/core.py", "def add(a, b):\n    return a + b\n")
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    )

    result = _run(tmp_path)

    assert result.passed is False
    assert result.executed is True
    assert EVIDENCE_MODULE_RESOLUTION in _categories(result)
    assert "PY_MODULE_NOT_FOUND" in _codes(result)
    assert any(p and p.endswith("hidden.py") for p in result.failed_paths), result.failed_paths


# ════════════════════════════════════════════════════════════════════════════
# 4. A pytest collection import error (a test importing a missing first-party
#    symbol) → RED. Exercises the collect layer (the resolver layer also flags
#    it; either attribution keeps the missing-symbol class present).
# ════════════════════════════════════════════════════════════════════════════


def test_parity_collection_import_error_is_red(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write(tmp_path, "src/app/core.py", "def add(a, b):\n    return a + b\n")
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import subtract\n\n\ndef test_sub():\n    assert subtract(3, 1) == 2\n",
    )

    result = _run(tmp_path)

    assert result.passed is False
    assert result.executed is True
    assert (
        EVIDENCE_MISSING_SYMBOL in _categories(result)
        or "PY_IMPORT_NAME_NOT_FOUND" in _codes(result)
    ), [(f.category, f.code) for f in result.findings]


# ════════════════════════════════════════════════════════════════════════════
# 5. A required .py scope that is empty / not observed → OracleScopeError
#    (the scope certifier hard-fails — RED, never a silent pass).
# ════════════════════════════════════════════════════════════════════════════


def test_parity_empty_source_scope_raises(tmp_path: Path) -> None:
    # Only a test tree exists; the required source root has zero .py.
    _write(tmp_path, "tests/__init__.py", "")
    _write(tmp_path, "tests/test_smoke.py", "def test_ok():\n    assert True\n")
    with pytest.raises(OracleScopeError):
        _run(tmp_path)


# ════════════════════════════════════════════════════════════════════════════
# 6. A collection failure caused ONLY by an uninstalled THIRD-PARTY dep is
#    TOLERATED (passed=True) — env state, NOT incoherence. Must STAY tolerated.
# ════════════════════════════════════════════════════════════════════════════


def test_parity_uninstalled_third_party_is_tolerated(tmp_path: Path) -> None:
    _scaffold(tmp_path)
    _write(
        tmp_path,
        "src/app/net.py",
        "import _codd_absent_dep_xyz  # noqa: F401  (third-party, never installed)\n\n\n"
        "def use():\n    return _codd_absent_dep_xyz.go()\n",
    )
    _write(
        tmp_path,
        "tests/test_net.py",
        "from app.net import use\n\n\ndef test_use():\n    assert use is not None\n",
    )

    result = _run(tmp_path)

    assert result.passed is True, (
        "an uninstalled third-party import must be benign (no false-RED); findings: "
        f"{[(f.code, f.message) for f in result.findings]}"
    )
    assert result.executed is True


def test_parity_third_party_import_in_source_is_tolerated(tmp_path: Path) -> None:
    """A first-party-resolving package whose source imports third-party/stdlib → PASS."""
    _scaffold(tmp_path)
    _write(
        tmp_path,
        "src/app/net.py",
        "from collections import OrderedDict\n"
        "from os.path import join\n\n\n"
        "def use():\n    return OrderedDict(), join('a', 'b')\n",
    )
    _write(
        tmp_path,
        "tests/test_net.py",
        "from app.net import use\n\n\ndef test_use():\n    d, p = use()\n    assert p\n",
    )

    result = _run(tmp_path)

    assert result.passed is True, [(f.code, f.message) for f in result.findings]


# ════════════════════════════════════════════════════════════════════════════
# 7. pytest itself missing / un-spawnable → environment_build_error RED
#    (NOT a benign pass — the collect layer could not run, so it proves nothing).
# ════════════════════════════════════════════════════════════════════════════


def test_parity_pytest_missing_is_environment_red(tmp_path: Path, monkeypatch) -> None:
    """If pytest cannot be collected, the gate honest-fails (never a silent green).

    Pins the anti-false-green core: pytest absent is an environment failure, never
    a pass. (Simulated by forcing the collect layer to report 'No module named
    pytest' — the gate must surface an environment_build_error and NOT pass.)
    """
    import codd.languages.adapters.oracle_python as oracle_python
    from codd.implement_oracle import (
        EVIDENCE_ENVIRONMENT_BUILD as ENV,
        ImplementOracleFinding,
        PythonToolRun,
    )

    _scaffold(tmp_path)
    _write(tmp_path, "src/app/core.py", "def ok():\n    return 1\n")
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import ok\n\n\ndef test_ok():\n    assert ok() == 1\n",
    )

    def _fake_collect(*args, **kwargs):
        return PythonToolRun(
            name="pytest_collect",
            executed=False,
            findings=(
                ImplementOracleFinding(
                    category=ENV,
                    code="pytest_not_installed",
                    message="pytest is not installed",
                ),
            ),
        )

    # The collect layer was relocated to the ``python-composite`` adapter (Contract
    # Kernel oracle dispatch §6). Patch it on the adapter module (the gate re-exports
    # the name for back-compat, but the adapter calls its own module-local function).
    monkeypatch.setattr(oracle_python, "_run_python_pytest_collect_layer", _fake_collect)

    result = _run(tmp_path)

    assert result.passed is False, "pytest-absent must not be a silent green"
    assert result.executed is True
    assert EVIDENCE_ENVIRONMENT_BUILD in _categories(result)
    assert any(f.code == "pytest_not_installed" for f in result.findings)


# ════════════════════════════════════════════════════════════════════════════
# 8. Public-API parity: the two ``__all__`` names tests/external code import stay
#    importable from codd.implement_oracle (delegating shim or re-export), and the
#    third-party-only collection classifier keeps its exact benign/honest verdict.
# ════════════════════════════════════════════════════════════════════════════


def test_parity_public_api_importable() -> None:
    from codd.implement_oracle import (  # noqa: F401
        certify_python_oracle_scope,
        normalize_python_tool_output,
    )

    assert callable(certify_python_oracle_scope)
    assert callable(normalize_python_tool_output)


def test_parity_third_party_only_collection_classifier() -> None:
    """The benign/honest boundary of the third-party-only collection verdict.

    ONLY a collection failure entirely attributable to non-first-party imports is
    benign; a first-party module-not-found, a cannot-import-name from a first-party
    module, a SyntaxError, an unattributable non-zero exit, or an under-accounted
    error set must stay honest (False). This is the anti-false-green core."""
    from codd.implement_oracle import _collection_failure_is_third_party_only as f

    fp = lambda m: m.split(".", 1)[0] == "app"  # noqa: E731

    third_party = (
        "src/app/net.py:1: in <module>\n    import requests\n"
        "E   ModuleNotFoundError: No module named 'requests'\n"
    )
    assert f(third_party, fp, 1) is True

    first_party_missing = (
        "tests/test_x.py:1: in <module>\n    from app.missing import y\n"
        "E   ModuleNotFoundError: No module named 'app.missing'\n"
    )
    assert f(first_party_missing, fp, 1) is False

    assert f("E   ImportError: cannot import name 'gone' from 'app.core'\n", fp, 1) is False
    assert f("E   SyntaxError: invalid syntax\n", fp, 1) is False
    assert f("non-zero exit, no parseable error\n", fp, 0) is False
    assert f("E   ModuleNotFoundError: No module named 'requests'\n", fp, 2) is False
