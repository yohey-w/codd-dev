"""Tests for the AST import-coherence gate (A-core anti-false-green).

Source + tests must share ONE package context. The gate runs BEFORE pytest and
FAILS HONESTLY when they disagree (the cross-vendor false-green: a test imports a
generated source module by BARE BASENAME, which only resolves via PYTHONPATH).
These tests cover :func:`codd.import_coherence.check_import_coherence`:

* a COHERENT package layout (package-absolute test imports) PASSES;
* a bare-basename test import FAILS;
* shadowing / source-outside-package / missing-init FAIL;
* the explicit opt-out is honored; the gate is never weakened silently;
* the real codex3 incoherence (flat src + bare-basename importlib) FAILS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.import_coherence import check_import_coherence
from codd.project_types import resolve_layout_profile, scaffold_layout


def _profile(name="todo-cli"):
    return resolve_layout_profile(
        language="python", project_name=name, source_dirs=["src/"], test_dirs=["tests/"]
    )


def _coherent_project(tmp_path: Path, name="todo-cli") -> None:
    """A correct src-layout package + a package-absolute test."""
    profile = _profile(name)
    pkg = tmp_path / profile.package_root
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "todo_store.py").write_text(
        "from .todo_model import make\n\n\ndef add(x):\n    return make(x)\n"
    )
    (pkg / "todo_model.py").write_text("def make(x):\n    return {'v': x}\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_store.py").write_text(
        f"from {profile.package_name}.todo_store import add\n\n\n"
        "def test_add():\n    assert add(1) == {'v': 1}\n"
    )


def _check(tmp_path, name="todo-cli", config=None):
    return check_import_coherence(
        tmp_path,
        language="python",
        project_name=name,
        source_dirs=["src/"],
        test_dirs=["tests/"],
        config=config,
    )


# ═══════════════════════════════════════════════════════════
# coherent layout passes
# ═══════════════════════════════════════════════════════════


class TestCoherentPasses:
    def test_package_absolute_layout_passes(self, tmp_path):
        _coherent_project(tmp_path)
        result = _check(tmp_path)
        assert result.passed, result.summary()
        assert result.findings == []

    def test_relative_imports_inside_package_are_coherent(self, tmp_path):
        # `from .todo_model import make` (level=1) must NOT be flagged.
        _coherent_project(tmp_path)
        result = _check(tmp_path)
        assert result.passed, result.summary()

    def test_no_profile_stack_is_passing_noop(self, tmp_path):
        result = check_import_coherence(tmp_path, language="rust", project_name="x")
        assert result.passed
        assert "no layout profile" in result.detail


# ═══════════════════════════════════════════════════════════
# bare-basename import fails (the core false-green)
# ═══════════════════════════════════════════════════════════


class TestBareBasenameFails:
    def test_bare_import_statement_fails(self, tmp_path):
        _coherent_project(tmp_path)
        (tmp_path / "tests" / "test_bad.py").write_text(
            "import todo_store\n\n\ndef test_x():\n    assert todo_store\n"
        )
        result = _check(tmp_path)
        assert not result.passed
        kinds = {f.kind for f in result.findings}
        assert "bare_basename_import" in kinds
        assert any("todo_store" in f.message for f in result.findings)

    def test_bare_from_import_fails(self, tmp_path):
        _coherent_project(tmp_path)
        (tmp_path / "tests" / "test_bad.py").write_text(
            "from todo_store import add\n\n\ndef test_x():\n    assert add\n"
        )
        result = _check(tmp_path)
        assert not result.passed
        assert any(f.kind == "bare_basename_import" for f in result.findings)

    def test_bare_importlib_string_fails(self, tmp_path):
        # The exact codex3 pattern: importlib.import_module("todo_store").
        _coherent_project(tmp_path)
        (tmp_path / "tests" / "test_bad.py").write_text(
            "import importlib\n\n\n"
            "def test_x():\n    importlib.import_module('todo_store')\n"
        )
        result = _check(tmp_path)
        assert not result.passed
        assert any(f.kind == "bare_basename_import" for f in result.findings)

    def test_package_absolute_importlib_string_passes(self, tmp_path):
        # importlib.import_module("todo_cli.todo_store") is coherent.
        _coherent_project(tmp_path)
        (tmp_path / "tests" / "test_ok2.py").write_text(
            "import importlib\n\n\n"
            "def test_x():\n    importlib.import_module('todo_cli.todo_store')\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()

    def test_stdlib_and_thirdparty_imports_not_flagged(self, tmp_path):
        # Only GENERATED source modules are flagged; stdlib/3p imports are fine.
        _coherent_project(tmp_path)
        (tmp_path / "tests" / "test_ok3.py").write_text(
            "import json\nimport os\nimport pytest\n\n\ndef test_x():\n    assert json\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()


# ═══════════════════════════════════════════════════════════
# layout violations fail
# ═══════════════════════════════════════════════════════════


class TestLayoutViolations:
    def test_source_outside_package_fails(self, tmp_path):
        # flat src/foo.py instead of src/<pkg>/foo.py
        profile = _profile()
        (tmp_path / profile.package_root).mkdir(parents=True)
        (tmp_path / profile.package_root / "__init__.py").write_text("")
        (tmp_path / "src" / "stray.py").write_text("X = 1\n")
        (tmp_path / "tests").mkdir()
        result = _check(tmp_path)
        assert not result.passed
        assert any(f.kind == "source_outside_package" for f in result.findings)

    def test_missing_package_init_fails(self, tmp_path):
        profile = _profile()
        pkg = tmp_path / profile.package_root
        pkg.mkdir(parents=True)
        # module present but NO __init__.py
        (pkg / "todo_store.py").write_text("def add(x):\n    return x\n")
        (tmp_path / "tests").mkdir()
        result = _check(tmp_path)
        assert not result.passed
        assert any(f.kind == "missing_package_init" for f in result.findings)

    def test_shadowing_module_fails(self, tmp_path):
        _coherent_project(tmp_path)
        # duplicate basename at the flat source root shadows the package module.
        (tmp_path / "src" / "todo_store.py").write_text("def add(x):\n    return x\n")
        result = _check(tmp_path)
        assert not result.passed
        kinds = {f.kind for f in result.findings}
        assert "shadowing_module" in kinds

    def test_manifest_source_root_mismatch_fails(self, tmp_path):
        _coherent_project(tmp_path)
        # pyproject declares a DIFFERENT setuptools where root.
        (tmp_path / "pyproject.toml").write_text(
            "[tool.setuptools.packages.find]\nwhere = [\"lib\"]\n", encoding="utf-8"
        )
        result = _check(tmp_path)
        assert not result.passed
        assert any(f.kind == "manifest_source_root_mismatch" for f in result.findings)


# ═══════════════════════════════════════════════════════════
# opt-out (explicit, never weakened silently)
# ═══════════════════════════════════════════════════════════


class TestOptOut:
    def test_opt_out_disables_gate(self, tmp_path):
        _coherent_project(tmp_path)
        (tmp_path / "tests" / "test_bad.py").write_text("import todo_store\n")
        # would fail, but the explicit opt-out passes it.
        result = _check(tmp_path, config={"coherence": {"import_coherence": False}})
        assert result.passed
        assert "disabled" in result.detail

    def test_default_is_on(self, tmp_path):
        _coherent_project(tmp_path)
        (tmp_path / "tests" / "test_bad.py").write_text("import todo_store\n")
        # no config / no opt-out → gate is ON and fails.
        result = _check(tmp_path, config={})
        assert not result.passed


# ═══════════════════════════════════════════════════════════
# real codex3 reproduction
# ═══════════════════════════════════════════════════════════


class TestCodex3Reproduction:
    def test_flat_src_plus_bare_importlib_fails_honestly(self, tmp_path):
        # Reproduce codex3 exactly: flat src/*.py with package-relative source
        # imports, tests resolving by bare basename first. This MUST fail (so the
        # project is REGENERATED, not silently passed via pythonpath=".").
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "todo_store.py").write_text(
            "from .todo_model import make\n\n\ndef add(x):\n    return make(x)\n"
        )
        (tmp_path / "src" / "todo_model.py").write_text("def make(x):\n    return {'v': x}\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_store.py").write_text(
            "import importlib\n\n\n"
            "def _mod():\n"
            "    for name in ('todo_store', 'todo_cli.todo_store'):\n"
            "        try:\n"
            "            return importlib.import_module(name)\n"
            "        except ImportError:\n"
            "            continue\n"
            "    raise AssertionError('nope')\n\n\n"
            "def test_add():\n    assert _mod()\n"
        )
        result = _check(tmp_path)
        assert not result.passed, result.summary()
        kinds = {f.kind for f in result.findings}
        # both the structural violation and the import-policy violation surface.
        assert "source_outside_package" in kinds
        assert "bare_basename_import" in kinds


# ═══════════════════════════════════════════════════════════
# Path-escape jail — scan.source_dirs / scan.test_dirs are user-
# controllable (codd.yaml). A ``../`` traversal or an in-root symlink
# whose target escapes must NEVER walk/read files OUTSIDE the project
# (no out-of-root content read as source modules or as test files).
# ═══════════════════════════════════════════════════════════


class TestPathEscapeJail:
    def _outside_with_violation(self, tmp_path: Path) -> Path:
        """A SIBLING dir holding a 'test' that, if read, would flag bare-basename."""
        outside = tmp_path.parent / (tmp_path.name + "_outside")
        outside.mkdir()
        # if this external file were read as a project test, it would flag
        # 'todo_store' (a real in-root source module) by bare basename.
        (outside / "test_evil.py").write_text("import todo_store\n", encoding="utf-8")
        return outside

    def test_parent_traversal_test_dir_is_not_read(self, tmp_path):
        _coherent_project(tmp_path)
        outside = self._outside_with_violation(tmp_path)
        result = check_import_coherence(
            tmp_path,
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=[f"../{outside.name}"],  # escapes the project root
        )
        # The escaping external test must NOT be read → no bare_basename finding.
        assert not any("test_evil" in f.path for f in result.findings), result.summary()

    def test_parent_traversal_source_dir_is_not_read(self, tmp_path):
        # An escaping source_dir must not pull external modules into the source set
        # (which could then false-flag an in-root test). External 'ext_mod.py' must
        # not become a known source module.
        _coherent_project(tmp_path)
        outside = tmp_path.parent / (tmp_path.name + "_src_outside")
        outside.mkdir()
        (outside / "ext_mod.py").write_text("X = 1\n", encoding="utf-8")
        (tmp_path / "tests" / "test_uses_ext.py").write_text(
            "import ext_mod\n", encoding="utf-8"
        )
        result = check_import_coherence(
            tmp_path,
            language="python",
            project_name="todo-cli",
            source_dirs=[f"../{outside.name}"],  # escapes the project root
            test_dirs=["tests/"],
        )
        # 'ext_mod' must NOT be a recognized source module (it lives outside root),
        # so the in-root test importing it is not flagged as a bare-basename of a
        # generated module.
        assert not any(
            f.kind == "bare_basename_import" and "ext_mod" in f.message
            for f in result.findings
        ), result.summary()

    def test_symlinked_test_dir_escaping_root_fails_closed(self, tmp_path):
        # An IN-ROOT test_root that is a SYMLINK whose target escapes the tree is
        # an invalid evidence root: the gate must FAIL (red), not silently skip
        # the off-root tree and "pass" by checking nothing (a false-green).
        _coherent_project(tmp_path)
        outside = self._outside_with_violation(tmp_path)
        link = tmp_path / "linked_tests"
        link.symlink_to(outside, target_is_directory=True)
        result = check_import_coherence(
            tmp_path,
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=["linked_tests"],
        )
        assert not result.passed, result.summary()
        assert any(f.kind == "evidence_root_escape" for f in result.findings), result.summary()

    def test_in_root_layout_unchanged(self, tmp_path):
        # ANTI-FALSE-RED: a normal in-root coherent project still passes, and a
        # genuine in-root bare-basename violation is still flagged.
        _coherent_project(tmp_path)
        assert _check(tmp_path).passed
        (tmp_path / "tests" / "test_bad.py").write_text(
            "import todo_store\n", encoding="utf-8"
        )
        result = _check(tmp_path)
        assert not result.passed
        assert any(f.kind == "bare_basename_import" for f in result.findings)
