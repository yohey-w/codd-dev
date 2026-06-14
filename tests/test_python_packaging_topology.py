"""Harness-owned Python packaging topology (backend-aware, canonical, anti-false-green).

The harness OWNS the Python packaging manifest fields that realize repository
topology — it must not let a generating model drift the pyproject manifest into
incoherence (2026-06 dogfood D11, first Claude library SUT: project ``calc-lib``,
model authored an internally-PERFECT ``src/calc/`` package + ``from calc import``
tests + hatchling, but the harness force-derived ``calc_lib``, rejected the
model's ``src/calc/`` as "outside output paths" → a contradictory DUPLICATE, and
its setuptools-only manifest gate let a topology-wrong hatch project pass — a
latent false green).

These tests cover the settled (GPT-5.5-validated) fix:

1. canonical package-name resolver — explicit ``project.package_name`` override >
   derive-from-the-model's-actual-single-package > project-name default;
2. the packaging ensurer runs EVEN WHEN ``[tool.pytest]`` is present (split from
   the pytest ensurer — the model owning test config never suppresses harness
   packaging coherence);
3. setuptools authoritative merge → coherent ``where``/``package-dir``;
4. hatchling authoritative merge → coherent ``[tool.hatch.build.targets.wheel]
   packages``;
5. NEVER cross backends (no setuptools table written into a hatch project);
6. the manifest gate now flags an incoherent HATCH packaging (closes the latent
   false-green);
7. source-routing reconciles to the canonical package with NO duplicate + imports
   coherent (the calc/calc_lib scenario);
8. domain intent (``[project]``, deps, ``[tool.pytest]``) is preserved byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.import_coherence import check_import_coherence
from codd.project_types import (
    _BACKEND_HATCHLING,
    _BACKEND_SETUPTOOLS,
    _detect_build_backend,
    _ensure_python_packaging,
    _ensure_python_test_runner,
    normalize_package_name,
    resolve_canonical_package_name,
    resolve_layout_profile,
)


def _profile(name="calc-lib", *, config=None, project_root=None, src="src", tests="tests"):
    return resolve_layout_profile(
        language="python",
        project_name=name,
        source_dirs=[src],
        test_dirs=[tests],
        config=config,
        project_root=project_root,
    )


# ═══════════════════════════════════════════════════════════
# 1. canonical package-name resolver (override > derive > default)
# ═══════════════════════════════════════════════════════════


class TestCanonicalPackageName:
    def test_default_is_normalized_project_name(self, tmp_path):
        # No override, no on-disk package → deterministic project-name default.
        result = resolve_canonical_package_name(
            "calc-lib", config={}, project_root=tmp_path, source_root="src"
        )
        assert result == "calc_lib" == normalize_package_name("calc-lib")

    def test_config_override_wins_over_project_name(self, tmp_path):
        config = {"project": {"name": "calc-lib", "package_name": "calc_core"}}
        result = resolve_canonical_package_name(
            "calc-lib", config=config, project_root=tmp_path, source_root="src"
        )
        assert result == "calc_core"

    def test_config_override_is_normalized(self, tmp_path):
        # An owner who writes a dist-style name still gets a valid identifier.
        config = {"project": {"name": "x", "package_name": "calc-core"}}
        result = resolve_canonical_package_name(
            "x", config=config, project_root=tmp_path, source_root="src"
        )
        assert result == "calc_core"

    def test_override_beats_an_existing_on_disk_package(self, tmp_path):
        # Override is highest precedence even when a different package exists.
        (tmp_path / "src" / "calc").mkdir(parents=True)
        (tmp_path / "src" / "calc" / "__init__.py").write_text("")
        (tmp_path / "src" / "calc" / "m.py").write_text("x = 1\n")
        config = {"project": {"name": "calc-lib", "package_name": "pinned_pkg"}}
        result = resolve_canonical_package_name(
            "calc-lib", config=config, project_root=tmp_path, source_root="src"
        )
        assert result == "pinned_pkg"

    def test_derive_from_actual_single_package(self, tmp_path):
        # The model authored ONE coherent package 'calc' (≠ normalize('calc-lib'));
        # with no override we adopt the model's actual package (artifact, not prose).
        pkg = tmp_path / "src" / "calc"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "evaluator.py").write_text("def f(): return 1\n")
        result = resolve_canonical_package_name(
            "calc-lib", config={}, project_root=tmp_path, source_root="src"
        )
        assert result == "calc"

    def test_ambiguous_two_packages_falls_back_to_default(self, tmp_path):
        # Two top-level packages → NOT unambiguous → project-name default (the
        # deterministic scaffold/merge then owns topology).
        for name in ("alpha", "beta"):
            pkg = tmp_path / "src" / name
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text("")
            (pkg / "m.py").write_text("x = 1\n")
        result = resolve_canonical_package_name(
            "calc-lib", config={}, project_root=tmp_path, source_root="src"
        )
        assert result == "calc_lib"

    def test_dir_without_init_is_not_a_package(self, tmp_path):
        # A plain dir (no __init__.py) is not a package candidate → default.
        (tmp_path / "src" / "data").mkdir(parents=True)
        (tmp_path / "src" / "data" / "x.txt").write_text("hi\n")
        result = resolve_canonical_package_name(
            "calc-lib", config={}, project_root=tmp_path, source_root="src"
        )
        assert result == "calc_lib"

    def test_profile_package_name_uses_canonical_derive(self, tmp_path):
        pkg = tmp_path / "src" / "calc"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "evaluator.py").write_text("def f(): return 1\n")
        profile = _profile("calc-lib", config={}, project_root=tmp_path)
        assert profile.package_name == "calc"
        assert profile.package_root == "src/calc"


# ═══════════════════════════════════════════════════════════
# 2. packaging ensurer runs EVEN WHEN [tool.pytest] present
# ═══════════════════════════════════════════════════════════


class TestPackagingSplitFromPytest:
    def test_packaging_reconciled_despite_strong_pytest_config(self, tmp_path):
        # The model owns [tool.pytest]; the harness STILL reconciles packaging.
        profile = _profile("todo-cli")
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["setuptools>=61"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            "[project]\n"
            'name = "todo-cli"\n'
            'version = "1.0.0"\n\n'
            "[tool.setuptools.packages.find]\n"
            'where = ["WRONG"]\n\n'
            "[tool.pytest.ini_options]\n"
            'testpaths = ["tests"]\n'
        )
        result = _ensure_python_test_runner(tmp_path, profile=profile)
        # Action reflects the packaging reconcile (NOT 'present' — the old all-or-
        # nothing bail).
        assert result.action == "augmented"
        text = (tmp_path / "pyproject.toml").read_text()
        # Packaging fixed to the profile source_root.
        assert 'where = ["src"]' in text
        assert '"WRONG"' not in text
        # The model's pytest config is untouched.
        assert "[tool.pytest.ini_options]" in text
        assert 'testpaths = ["tests"]' in text

    def test_strong_pytest_no_packaging_change_is_noop(self, tmp_path):
        # When packaging is ALREADY coherent and pytest is strong → true no-op.
        profile = _profile("todo-cli")
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["setuptools>=61"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            "[project]\n"
            'name = "todo-cli"\n'
            'version = "1.0.0"\n\n'
            "[tool.setuptools]\n"
            'package-dir = {"" = "src"}\n\n'
            "[tool.setuptools.packages.find]\n"
            'where = ["src"]\n\n'
            "[tool.pytest.ini_options]\n"
            'testpaths = ["tests"]\n'
        )
        before = (tmp_path / "pyproject.toml").read_text()
        result = _ensure_python_test_runner(tmp_path, profile=profile)
        assert result.action == "present"
        assert (tmp_path / "pyproject.toml").read_text() == before  # byte-exact


# ═══════════════════════════════════════════════════════════
# 3. setuptools authoritative merge → coherent where/package-dir
# ═══════════════════════════════════════════════════════════


class TestSetuptoolsMerge:
    def test_forces_coherent_where_and_package_dir(self, tmp_path):
        profile = _profile("calc-lib")  # package = calc_lib
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["setuptools>=61"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            "[project]\n"
            'name = "calc-lib"\n'
            'version = "1.0.0"\n'
            'dependencies = ["click>=8"]\n\n'
            "[tool.setuptools.packages.find]\n"
            'where = ["src/calc_lib"]\n'  # the D11 mismatch
        )
        result = _ensure_python_packaging(tmp_path, profile=profile)
        assert result.action == "augmented"
        text = (tmp_path / "pyproject.toml").read_text()
        assert 'where = ["src"]' in text
        assert 'where = ["src/calc_lib"]' not in text
        assert 'package-dir = {"" = "src"}' in text
        # Domain intent preserved byte-for-byte.
        assert 'dependencies = ["click>=8"]' in text
        assert 'name = "calc-lib"' in text

    def test_idempotent(self, tmp_path):
        profile = _profile("calc-lib")
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["setuptools>=61"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            "[project]\n"
            'name = "calc-lib"\n'
            'version = "1.0.0"\n\n'
            "[tool.setuptools.packages.find]\n"
            'where = ["src/calc_lib"]\n'
        )
        _ensure_python_packaging(tmp_path, profile=profile)
        once = (tmp_path / "pyproject.toml").read_text()
        result2 = _ensure_python_packaging(tmp_path, profile=profile)
        assert result2.action == "present"
        assert (tmp_path / "pyproject.toml").read_text() == once


# ═══════════════════════════════════════════════════════════
# 4. hatchling authoritative merge → coherent wheel packages
# ═══════════════════════════════════════════════════════════


class TestHatchlingMerge:
    def test_forces_coherent_wheel_packages(self, tmp_path):
        # Model wrote src/calc/ + hatchling; canonical derives 'calc'.
        pkg = tmp_path / "src" / "calc"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "evaluator.py").write_text("def f(): return 1\n")
        profile = _profile("calc-lib", config={}, project_root=tmp_path)
        assert profile.package_name == "calc"
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n\n'
            "[project]\n"
            'name = "calc-lib"\n'
            'version = "1.0.0"\n\n'
            "[tool.coverage.run]\n"
            'source = ["calc"]\n\n'
            "[tool.pytest.ini_options]\n"
            'testpaths = ["tests"]\n'
        )
        result = _ensure_python_packaging(tmp_path, profile=profile)
        assert result.action == "augmented"
        text = (tmp_path / "pyproject.toml").read_text()
        assert 'packages = ["src/calc"]' in text
        assert "[tool.hatch.build.targets.wheel]" in text
        # Domain intent preserved.
        assert 'source = ["calc"]' in text
        assert "[tool.pytest.ini_options]" in text

    def test_fixes_wrong_wheel_packages(self, tmp_path):
        profile = _profile("calc-lib")  # canonical = calc_lib (no on-disk pkg)
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n\n'
            "[project]\n"
            'name = "calc-lib"\n'
            'version = "1.0.0"\n\n'
            "[tool.hatch.build.targets.wheel]\n"
            'packages = ["src/WRONG"]\n'
        )
        result = _ensure_python_packaging(tmp_path, profile=profile)
        assert result.action == "augmented"
        text = (tmp_path / "pyproject.toml").read_text()
        assert 'packages = ["src/calc_lib"]' in text
        assert '"src/WRONG"' not in text


# ═══════════════════════════════════════════════════════════
# 5. NEVER cross backends
# ═══════════════════════════════════════════════════════════


class TestNeverCrossBackends:
    def test_no_setuptools_table_in_hatch_project(self, tmp_path):
        profile = _profile("calc-lib")
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n\n'
            "[project]\n"
            'name = "calc-lib"\n'
            'version = "1.0.0"\n\n'
            "[tool.hatch.build.targets.wheel]\n"
            'packages = ["src/WRONG"]\n'
        )
        _ensure_python_packaging(tmp_path, profile=profile)
        text = (tmp_path / "pyproject.toml").read_text()
        # NEVER a setuptools table in a hatch project.
        assert "[tool.setuptools" not in text
        assert "setuptools.build_meta" not in text

    def test_no_hatch_table_in_setuptools_project(self, tmp_path):
        profile = _profile("calc-lib")
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["setuptools>=61"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            "[project]\n"
            'name = "calc-lib"\n'
            'version = "1.0.0"\n\n'
            "[tool.setuptools.packages.find]\n"
            'where = ["WRONG"]\n'
        )
        _ensure_python_packaging(tmp_path, profile=profile)
        text = (tmp_path / "pyproject.toml").read_text()
        assert "[tool.hatch" not in text

    def test_unknown_backend_declined_not_edited(self, tmp_path):
        # flit/pdm/poetry are not setuptools/hatchling → DECLINE (the manifest gate
        # is the honest backstop; never guess a packaging table).
        profile = _profile("calc-lib")
        original = (
            "[build-system]\n"
            'requires = ["flit_core>=3.2"]\n'
            'build-backend = "flit_core.buildapi"\n\n'
            "[project]\n"
            'name = "calc-lib"\n'
            'version = "1.0.0"\n'
        )
        (tmp_path / "pyproject.toml").write_text(original)
        result = _ensure_python_packaging(tmp_path, profile=profile)
        assert result.action == "present"
        assert (tmp_path / "pyproject.toml").read_text() == original  # untouched

    def test_detect_build_backend(self):
        assert _detect_build_backend(
            '[build-system]\nbuild-backend = "setuptools.build_meta"\n'
        ) == _BACKEND_SETUPTOOLS
        assert _detect_build_backend(
            '[build-system]\nbuild-backend = "hatchling.build"\n'
        ) == _BACKEND_HATCHLING
        # Unknown backend → the raw token (caller declines).
        assert _detect_build_backend(
            '[build-system]\nbuild-backend = "flit_core.buildapi"\n'
        ) == "flit_core.buildapi"
        # No build-system at all → None (fresh file path).
        assert _detect_build_backend("") is None


# ═══════════════════════════════════════════════════════════
# 6. manifest gate flags incoherent HATCH packaging (false-green closed)
# ═══════════════════════════════════════════════════════════


def _hatch_project(tmp_path: Path, *, wheel_packages: str, pkg_dir: str = "calc") -> None:
    pkg = tmp_path / "src" / pkg_dir
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def f():\n    return 1\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_core.py").write_text(
        f"from {pkg_dir}.core import f\n\n\ndef test_f():\n    assert f() == 1\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[project]\n"
        f'name = "{pkg_dir}"\n'
        'version = "0.0.0"\n\n'
        "[tool.hatch.build.targets.wheel]\n"
        f"packages = [{wheel_packages}]\n"
    )


class TestManifestGateHatch:
    def test_incoherent_hatch_wheel_packages_fails(self, tmp_path):
        _hatch_project(tmp_path, wheel_packages='"src/WRONG"')
        result = check_import_coherence(
            tmp_path, language="python", project_name="calc",
            source_dirs=["src/"], test_dirs=["tests/"], config={},
        )
        assert not result.passed
        kinds = {f.kind for f in result.findings}
        assert "manifest_hatch_packages_mismatch" in kinds

    def test_coherent_hatch_wheel_packages_passes(self, tmp_path):
        _hatch_project(tmp_path, wheel_packages='"src/calc"')
        result = check_import_coherence(
            tmp_path, language="python", project_name="calc",
            source_dirs=["src/"], test_dirs=["tests/"], config={},
        )
        assert result.passed, result.summary()

    def test_setuptools_where_mismatch_still_fails(self, tmp_path):
        # Regression guard: the original setuptools check is intact.
        pkg = tmp_path / "src" / "calc"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "core.py").write_text("def f():\n    return 1\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("")
        (tests / "test_core.py").write_text(
            "from calc.core import f\n\n\ndef test_f():\n    assert f() == 1\n"
        )
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["setuptools>=61"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            "[project]\n"
            'name = "calc"\n'
            'version = "0.0.0"\n\n'
            "[tool.setuptools.packages.find]\n"
            'where = ["src/calc"]\n'  # too deep — excludes source_root 'src'
        )
        result = check_import_coherence(
            tmp_path, language="python", project_name="calc",
            source_dirs=["src/"], test_dirs=["tests/"], config={},
        )
        assert not result.passed
        kinds = {f.kind for f in result.findings}
        assert "manifest_source_root_mismatch" in kinds


# ═══════════════════════════════════════════════════════════
# 7. source-routing reconciles to canonical, NO duplicate, imports coherent
# ═══════════════════════════════════════════════════════════


class TestSourceRoutingReconcile:
    def test_route_accepts_model_package_and_canonical(self, tmp_path):
        # Model already authored src/calc/ (its own package, ≠ normalize('calc-lib')).
        from codd.greenfield.pipeline import _route_source_into_package

        pkg = tmp_path / "src" / "calc"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "evaluator.py").write_text("def f(): return 1\n")
        config = {
            "project": {"name": "calc-lib", "language": "python"},
            "scan": {"source_dirs": ["src/"], "test_dirs": ["tests/"]},
        }
        routed = _route_source_into_package(config, ["src"], project_root=tmp_path)
        # canonical derives 'calc' → package_root src/calc is the primary dest,
        # AND the bare source_root 'src' is accepted (so the model's own package is
        # never dropped as "outside output paths"), plus the test root.
        assert "src/calc" in routed
        assert "src" in routed
        assert "tests" in routed
        # No reference to the wrong forced 'calc_lib' package.
        assert "src/calc_lib" not in routed

    def test_route_default_when_no_existing_package(self, tmp_path):
        from codd.greenfield.pipeline import _route_source_into_package

        config = {
            "project": {"name": "todo-cli", "language": "python"},
            "scan": {"source_dirs": ["src/"], "test_dirs": ["tests/"]},
        }
        routed = _route_source_into_package(config, ["src"], project_root=tmp_path)
        assert "src/todo_cli" in routed
        assert "tests" in routed

    def test_end_state_no_duplicate_and_imports_coherent(self, tmp_path):
        # The full calc/calc_lib scenario END STATE: ONE package 'calc', tests
        # import 'from calc', hatchling wheel reconciled to src/calc → the whole
        # import-coherence gate (incl. shadowing/duplicate + manifest) PASSES.
        pkg = tmp_path / "src" / "calc"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            "from .errors import CalcError\nfrom .evaluator import evaluate\n"
            '__all__ = ["evaluate", "CalcError"]\n'
        )
        (pkg / "errors.py").write_text("class CalcError(ValueError):\n    pass\n")
        (pkg / "evaluator.py").write_text(
            "from .errors import CalcError\n\n\ndef evaluate(expr):\n"
            "    if not expr:\n        raise CalcError('empty')\n    return 1\n"
        )
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("")
        (tests / "test_eval.py").write_text(
            "from calc import evaluate, CalcError\n\n\n"
            "def test_eval():\n    assert evaluate('1') == 1\n"
        )
        # Reconcile packaging (hatchling), then run the gate.
        profile = _profile("calc-lib", config={}, project_root=tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n\n'
            "[project]\n"
            'name = "calc-lib"\n'
            'version = "1.0.0"\n'
        )
        _ensure_python_packaging(tmp_path, profile=profile)
        result = check_import_coherence(
            tmp_path, language="python", project_name="calc-lib",
            source_dirs=["src/"], test_dirs=["tests/"], config={},
        )
        assert result.passed, result.summary()
        # No duplicate package: only ONE top-level package under src.
        top = [p.name for p in (tmp_path / "src").iterdir() if (p / "__init__.py").exists()]
        assert top == ["calc"]
