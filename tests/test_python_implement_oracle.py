"""Certification fixtures for the Python COMPOSITE implement-time oracle.

The Python ``LayoutProfile`` has no single compiler (no ``tsc --noEmit``), so its
implement-time anti-false-green oracle is a COMPOSITE of three hard layers, run
BEFORE pytest while the SUT can still edit every file (design:
``/tmp/gpt_python_oracle.txt``, GPT-5.5 Pro, 2026-06-17):

  1. **compile** — in-process ``compile()`` over ALL source+test ``.py``
     (SyntaxError / IndentationError / TabError / decode errors).
  2. **first-party imports** (THE CORE / KEYSTONE) — a static AST resolver over
     ALL source+test ``.py`` that proves every FIRST-PARTY module + imported
     symbol exists. This is the ONLY layer that catches a ``src/app/hidden.py:
     from .missing import X`` that NO test imports — invisible to py_compile
     (no resolution) and to ``--collect-only`` (never imported).
  3. **pytest --collect-only** — the test-surface importability layer.

FALSE-RED avoidance is load-bearing: ``if TYPE_CHECKING:`` imports, ``try/except
ImportError`` guarded imports, third-party imports, and non-literal dynamic
imports are all IGNORED (PASS), never a hard fail — mirroring the
"PROVABLY absent → fail; unknown → never fail" policy of
``codd/test_import_coherence.py``.

The PASS fixtures install a ``conftest.py`` ``sys.path`` shim so ``--collect-only``
resolves the package (the real greenfield flow editable-installs it; the shim is
the deterministic, dependency-free test equivalent). The RED import-resolution
fixtures assert on the import-resolver layer's findings, which are produced
STATICALLY regardless of whether the package is installed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codd.implement_oracle import (
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    OracleScopeError,
    certify_python_oracle_scope,
    run_implement_oracle_gate,
)
from codd.project_types import resolve_layout_profile


# ─────────────────────────────────────────────────────────────
# Fixture builders
# ─────────────────────────────────────────────────────────────


_PACKAGE = "app"


def _profile():
    profile = resolve_layout_profile(language="python", project_name=_PACKAGE)
    assert profile is not None and profile.implement_oracle is not None
    assert profile.implement_oracle.kind == "composite"
    return profile


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _scaffold(root: Path, *, with_path_shim: bool = False) -> None:
    """A minimal src-layout Python project (package ``app`` under ``src``)."""
    _write(root, "src/app/__init__.py", "")
    _write(root, "tests/__init__.py", "")
    if with_path_shim:
        # Make ``--collect-only`` resolve the package WITHOUT an editable install
        # (the deterministic, dependency-free equivalent of the greenfield flow's
        # ``pip install -e .``). conftest at the project root runs before collection.
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


# ═════════════════════════════════════════════════════════════
# 1. KEYSTONE — a hidden source module with a missing FIRST-PARTY import
#    that NO test imports → composite oracle RED (module_resolution).
# ═════════════════════════════════════════════════════════════


def test_hidden_missing_module_is_red_keystone(tmp_path: Path) -> None:
    """``src/app/hidden.py: from .missing import X`` (unimported) → RED.

    THE keystone false-green: py_compile does not resolve the import and
    collect-only never imports the module, so ONLY the first-party resolver
    proves the module absent. This is the Python equivalent of TS TS2307.
    """
    _scaffold(tmp_path, with_path_shim=True)
    _write(tmp_path, "src/app/hidden.py", "from .missing import X\n")
    _write(tmp_path, "src/app/core.py", "def add(a, b):\n    return a + b\n")
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    )

    result = _run(tmp_path)

    assert result.executed is True
    assert result.passed is False, "a hidden missing first-party module must RED"
    assert result.category_counts().get(EVIDENCE_MODULE_RESOLUTION, 0) >= 1
    assert "PY_MODULE_NOT_FOUND" in _codes(result)
    # The finding names the offending source file (the importer).
    assert any(p and p.endswith("hidden.py") for p in result.failed_paths), result.failed_paths


# ═════════════════════════════════════════════════════════════
# 2. A module that EXISTS but lacks the imported symbol → RED (missing_symbol).
# ═════════════════════════════════════════════════════════════


def test_missing_imported_symbol_is_red(tmp_path: Path) -> None:
    """``from app.helpers import missing_symbol`` (helpers.py lacks it) → RED."""
    _scaffold(tmp_path, with_path_shim=True)
    _write(tmp_path, "src/app/helpers.py", "def present():\n    return 1\n")
    _write(tmp_path, "src/app/user.py", "from app.helpers import missing_symbol\n")
    _write(
        tmp_path,
        "tests/test_helpers.py",
        "from app.helpers import present\n\n\ndef test_present():\n    assert present() == 1\n",
    )

    result = _run(tmp_path)

    assert result.passed is False
    assert result.category_counts().get(EVIDENCE_MISSING_SYMBOL, 0) >= 1
    assert "PY_IMPORT_NAME_NOT_FOUND" in _codes(result)


# ═════════════════════════════════════════════════════════════
# 3. ``if TYPE_CHECKING:`` imports are IGNORED (PASS) — runtime oracle.
# ═════════════════════════════════════════════════════════════


def test_type_checking_import_is_ignored(tmp_path: Path) -> None:
    """A missing module imported ONLY under ``if TYPE_CHECKING:`` does NOT RED."""
    _scaffold(tmp_path, with_path_shim=True)
    _write(
        tmp_path,
        "src/app/models.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from app.not_real_at_runtime import Thing\n\n\n"
        "def make() -> 'Thing':\n"
        "    return object()  # type: ignore[return-value]\n",
    )
    _write(
        tmp_path,
        "tests/test_models.py",
        "from app.models import make\n\n\ndef test_make():\n    assert make() is not None\n",
    )

    result = _run(tmp_path)

    assert result.passed is True, (
        f"TYPE_CHECKING-only import must be ignored (no false-RED); findings: "
        f"{[(f.code, f.message) for f in result.findings]}"
    )


# ═════════════════════════════════════════════════════════════
# 4. ``try: from .optional import x except ImportError:`` → IGNORED (PASS).
# ═════════════════════════════════════════════════════════════


def test_guarded_optional_import_is_ignored(tmp_path: Path) -> None:
    """A guarded (try/except ImportError) FIRST-PARTY import does NOT RED."""
    _scaffold(tmp_path, with_path_shim=True)
    _write(
        tmp_path,
        "src/app/plugins.py",
        "try:\n"
        "    from .optional import plugin\n"
        "except ImportError:\n"
        "    plugin = None\n",
    )
    _write(
        tmp_path,
        "tests/test_plugins.py",
        "from app.plugins import plugin\n\n\ndef test_plugin():\n    assert plugin is None\n",
    )

    result = _run(tmp_path)

    assert result.passed is True, (
        f"guarded optional import must be skipped (no false-RED); findings: "
        f"{[(f.code, f.message) for f in result.findings]}"
    )


# ═════════════════════════════════════════════════════════════
# 5. A fully-coherent project → PASS (all three layers green).
# ═════════════════════════════════════════════════════════════


def test_clean_project_passes(tmp_path: Path) -> None:
    """All imports resolve, every symbol exists, tests collect → PASS."""
    _scaffold(tmp_path, with_path_shim=True)
    _write(tmp_path, "src/app/helpers.py", "def add(a, b):\n    return a + b\n")
    _write(
        tmp_path,
        "src/app/core.py",
        "from .helpers import add\n"
        "from app.helpers import add as add2\n\n\n"
        "def total(xs):\n"
        "    acc = 0\n"
        "    for x in xs:\n"
        "        acc = add(acc, x)\n"
        "    return acc\n",
    )
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import total\n\n\ndef test_total():\n    assert total([1, 2, 3]) == 6\n",
    )

    result = _run(tmp_path)

    assert result.passed is True, (
        f"a coherent project must PASS; findings: "
        f"{[(f.category, f.code, f.message) for f in result.findings]}"
    )
    assert result.executed is True


# ═════════════════════════════════════════════════════════════
# 6. Empty source_root → OracleScopeError (anti-false-green: empty scope).
# ═════════════════════════════════════════════════════════════


def test_empty_source_root_is_scope_error(tmp_path: Path) -> None:
    """A required source_root with ZERO .py is a HARD FAIL (not a silent pass)."""
    profile = _profile()
    # Only a test tree exists; the source root has no .py.
    _write(tmp_path, "tests/__init__.py", "")
    _write(tmp_path, "tests/test_smoke.py", "def test_ok():\n    assert True\n")
    with pytest.raises(OracleScopeError) as exc:
        certify_python_oracle_scope(tmp_path, profile, profile.implement_oracle)
    assert "empty" in str(exc.value).lower() or "no .py" in str(exc.value).lower()


def test_gate_propagates_scope_error_on_empty_source(tmp_path: Path) -> None:
    """The gate itself surfaces the empty-scope hard fail (not a green no-op)."""
    _write(tmp_path, "tests/__init__.py", "")
    _write(tmp_path, "tests/test_smoke.py", "def test_ok():\n    assert True\n")
    with pytest.raises(OracleScopeError):
        _run(tmp_path)


# ═════════════════════════════════════════════════════════════
# 7. Third-party imports (not first-party) are IGNORED (PASS).
# ═════════════════════════════════════════════════════════════


def test_third_party_import_is_ignored(tmp_path: Path) -> None:
    """``import requests`` / stdlib imports are NOT first-party → never RED."""
    _scaffold(tmp_path, with_path_shim=True)
    _write(
        tmp_path,
        "src/app/net.py",
        "import requests  # noqa: F401  (third-party, not installed — must be ignored)\n"
        "from collections import OrderedDict\n"
        "from os.path import join\n\n\n"
        "def use():\n"
        "    return OrderedDict(), join('a', 'b')\n",
    )
    _write(
        tmp_path,
        "tests/test_net.py",
        "from app.net import use\n\n\ndef test_use():\n    d, p = use()\n    assert p\n",
    )

    result = _run(tmp_path)

    assert result.passed is True, (
        f"third-party / stdlib imports must be ignored (no false-RED); findings: "
        f"{[(f.code, f.message) for f in result.findings]}"
    )


def test_uninstalled_third_party_import_is_benign_via_collection(tmp_path: Path) -> None:
    """A SUT importing a third-party dep that is NOT installed at implement time
    must NOT false-RED — even though ``pytest --collect-only`` exits non-zero on the
    ModuleNotFoundError. First-party importability is proven by the resolver layer;
    the missing dependency is an environment concern. (Reproduces the clean-CI
    failure that the env-dependent ``import requests`` test could not, by using a
    module guaranteed absent in every environment.)
    """
    _scaffold(tmp_path, with_path_shim=True)
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


def test_collection_failure_third_party_only_classifier() -> None:
    """Unit-lock the benign/honest boundary of ``_collection_failure_is_third_party_only``.

    Anti-false-green: ONLY a collection failure entirely attributable to
    non-first-party imports is benign; a first-party module-not-found, a
    cannot-import-name from a first-party module, a SyntaxError, an unattributable
    non-zero exit, or an under-accounted error set must stay honest (False)."""
    from codd.implement_oracle import _collection_failure_is_third_party_only as f

    fp = lambda m: m.split(".", 1)[0] == "app"  # noqa: E731  (first-party == package "app")

    third_party = (
        "src/app/net.py:1: in <module>\n    import requests\n"
        "E   ModuleNotFoundError: No module named 'requests'\n"
    )
    assert f(third_party, fp, 1) is True  # only cause is an uninstalled third-party dep

    first_party_missing = (
        "tests/test_x.py:1: in <module>\n    from app.missing import y\n"
        "E   ModuleNotFoundError: No module named 'app.missing'\n"
    )
    assert f(first_party_missing, fp, 1) is False  # a real first-party module is absent

    first_party_symbol = "E   ImportError: cannot import name 'gone' from 'app.core'\n"
    assert f(first_party_symbol, fp, 1) is False  # real first-party symbol error

    syntax = "E   SyntaxError: invalid syntax\n"
    assert f(syntax, fp, 1) is False  # a syntax error is always real

    assert f("non-zero exit, no parseable error\n", fp, 0) is False  # unattributable → honest

    under_accounted = (
        "E   ModuleNotFoundError: No module named 'requests'\n"  # explains only ONE of two files
    )
    assert f(under_accounted, fp, 2) is False  # 2 errored files, 1 third-party cause → honest


# ═════════════════════════════════════════════════════════════
# 8. A syntax error in a source file → RED (compile layer).
# ═════════════════════════════════════════════════════════════


def test_syntax_error_is_red(tmp_path: Path) -> None:
    """A SyntaxError in generated source is caught by the in-process compile layer."""
    _scaffold(tmp_path, with_path_shim=True)
    _write(tmp_path, "src/app/broken.py", "def f(:\n    return 1\n")  # invalid syntax
    _write(tmp_path, "src/app/core.py", "def ok():\n    return 1\n")
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import ok\n\n\ndef test_ok():\n    assert ok() == 1\n",
    )

    result = _run(tmp_path)

    assert result.passed is False
    assert any(f.code in ("SyntaxError", "IndentationError", "TabError") for f in result.findings), (
        [(f.code, f.message) for f in result.findings]
    )
    assert any(p and p.endswith("broken.py") for p in result.failed_paths), result.failed_paths


# ═════════════════════════════════════════════════════════════
# 9. A test module importing a non-existent helper symbol → RED at collection.
#    (Exercises the pytest --collect-only layer specifically.)
# ═════════════════════════════════════════════════════════════


def test_pytest_collection_error_is_red(tmp_path: Path) -> None:
    """A test that fails to import (bad symbol) is RED via the collect layer.

    The import-resolver layer ALSO flags this (the test imports a first-party
    symbol that does not exist), but the assertion targets the collection
    surface: a non-importable test must not pass the gate.
    """
    _scaffold(tmp_path, with_path_shim=True)
    _write(tmp_path, "src/app/core.py", "def add(a, b):\n    return a + b\n")
    _write(
        tmp_path,
        "tests/test_core.py",
        # ``subtract`` does not exist in app.core → ImportError at collection.
        "from app.core import subtract\n\n\ndef test_sub():\n    assert subtract(3, 1) == 2\n",
    )

    result = _run(tmp_path)

    assert result.passed is False
    # Either layer may attribute it; assert the missing-symbol class is present.
    assert (
        result.category_counts().get(EVIDENCE_MISSING_SYMBOL, 0) >= 1
        or "PY_IMPORT_NAME_NOT_FOUND" in _codes(result)
    ), [(f.category, f.code, f.message) for f in result.findings]


# ═════════════════════════════════════════════════════════════
# 10. pytest absent → environment_build_error (NOT a silent skip / green).
# ═════════════════════════════════════════════════════════════


def test_pytest_missing_is_environment_error_not_green(tmp_path: Path, monkeypatch) -> None:
    """If pytest cannot be collected, the gate honest-fails (never a silent green).

    Simulated by pointing the collect layer's interpreter at a python with no
    pytest (here: force the subprocess to report 'No module named pytest' by
    running a stub). We assert the gate does NOT pass and records an
    environment_build_error — skip == unverified, never green.
    """
    import codd.implement_oracle as mod

    _scaffold(tmp_path, with_path_shim=True)
    _write(tmp_path, "src/app/core.py", "def ok():\n    return 1\n")
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import ok\n\n\ndef test_ok():\n    assert ok() == 1\n",
    )

    from codd.implement_oracle import PythonToolRun, EVIDENCE_ENVIRONMENT_BUILD, ImplementOracleFinding

    def _fake_collect(project_root, profile, scope, config):
        return PythonToolRun(
            name="pytest_collect",
            executed=False,
            findings=(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="pytest_not_installed",
                    message="pytest is not installed",
                ),
            ),
        )

    monkeypatch.setattr(mod, "_run_python_pytest_collect_layer", _fake_collect)

    result = _run(tmp_path)

    assert result.passed is False, "pytest-absent must not be a silent green"
    assert any(f.code == "pytest_not_installed" for f in result.findings)


# ═════════════════════════════════════════════════════════════
# 11. Profile wiring: the Python profile now declares the composite oracle.
# ═════════════════════════════════════════════════════════════


def test_python_profile_declares_composite_oracle() -> None:
    profile = _profile()
    spec = profile.implement_oracle
    assert spec.command == "python-composite"
    assert spec.kind == "composite"
    assert spec.requires_node_install is False
    assert spec.scope.require_source_root is True
    assert spec.scope.require_test_root is True
    # Serialized for diagnostics / doctor surfaces.
    assert profile.to_dict()["implement_oracle"]["kind"] == "composite"


# ═════════════════════════════════════════════════════════════
# 12. Optional name-lint: required mode honest-fails when ruff/pyflakes absent.
# ═════════════════════════════════════════════════════════════


def test_name_lint_required_without_tool_is_environment_error(tmp_path: Path) -> None:
    """``python_name_lint: required`` with no ruff/pyflakes → environment error.

    Default ``optional`` SKIPS (no false-RED, no false coverage claim); the
    explicit ``required`` mode turns the missing tool into an honest environment
    failure (never a silent green). Skipped automatically if a lint tool IS
    present in the environment (then there is nothing to honest-fail about).
    """
    import importlib.util

    if shutil.which("ruff") is not None or importlib.util.find_spec("pyflakes") is not None:
        pytest.skip("ruff/pyflakes IS available — the required-but-absent path cannot be exercised")

    _scaffold(tmp_path, with_path_shim=True)
    _write(tmp_path, "src/app/core.py", "def ok():\n    return 1\n")
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import ok\n\n\ndef test_ok():\n    assert ok() == 1\n",
    )

    result = _run(tmp_path, config={"implement": {"python_name_lint": "required"}})

    assert result.passed is False
    assert any(f.code == "name_lint_unavailable" for f in result.findings), (
        [(f.code, f.message) for f in result.findings]
    )


# ═════════════════════════════════════════════════════════════
# PEP 562 module-level __getattr__: a named import from a module that
# dynamically provides attributes is statically UNDECIDABLE → not a false-RED.
# (normal-missing-symbol RED and namespace-package PASS are covered above.)
# ═════════════════════════════════════════════════════════════


def test_module_getattr_named_import_is_not_red(tmp_path: Path) -> None:
    """``from app.dynamic import compute`` where dynamic.py has module ``__getattr__`` → PASS."""
    _scaffold(tmp_path, with_path_shim=True)
    _write(
        tmp_path,
        "src/app/dynamic.py",
        "def __getattr__(name):\n    def _impl(*a, **k):\n        return name\n    return _impl\n",
    )
    _write(tmp_path, "src/app/core.py", "from app.dynamic import compute\n\n\ndef run():\n    return compute()\n")
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import run\n\n\ndef test_run():\n    assert run() == 'compute'\n",
    )

    result = _run(tmp_path)

    assert result.passed is True, [(f.code, f.message) for f in result.findings]


def test_dir_only_does_not_excuse_missing_symbol(tmp_path: Path) -> None:
    """``__dir__`` controls ``dir()`` display, NOT attribute lookup — a ``__dir__``-only
    module (no ``__getattr__``) does NOT excuse a missing symbol: stays RED."""
    _scaffold(tmp_path, with_path_shim=True)
    _write(tmp_path, "src/app/dyn.py", "def __dir__():\n    return ['compute']\n")
    _write(tmp_path, "src/app/core.py", "from app.dyn import compute\n")
    _write(tmp_path, "tests/test_smoke.py", "def test_ok():\n    assert True\n")

    result = _run(tmp_path)

    assert result.passed is False
    assert "PY_IMPORT_NAME_NOT_FOUND" in _codes(result)


def test_getattr_excuse_is_direct_target_only_not_via_reexport(tmp_path: Path) -> None:
    """The ``__getattr__`` excuse covers ONLY a DIRECT named import from the bearer.
    A re-exporting facade (no own ``__getattr__``) does NOT inherit it — so a symbol
    only the bearer provides dynamically, imported from the FACADE, stays RED. This
    keeps the false-GREEN surface narrow (``provided_names`` is not widened to UNKNOWN)."""
    _scaffold(tmp_path, with_path_shim=True)
    _write(tmp_path, "src/app/dynamic.py", "def __getattr__(name):\n    return name\n")
    _write(tmp_path, "src/app/facade.py", "from app.dynamic import *\n")
    _write(tmp_path, "src/app/core.py", "from app.facade import gen_thing\n")
    _write(tmp_path, "tests/test_smoke.py", "def test_ok():\n    assert True\n")

    result = _run(tmp_path)

    assert result.passed is False
    assert "PY_IMPORT_NAME_NOT_FOUND" in _codes(result)
