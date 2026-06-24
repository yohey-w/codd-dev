"""Tests for the test-helper SYMBOL-import coherence gate (A-core anti-false-green).

A sibling to ``test_import_coherence``. Where the source/test package gate proves
source + tests agree on import *context*, THIS gate proves that every symbol a
generated test imports from an in-test-tree helper (a sibling test module, a
helper package / subpackage ``__init__``, or ``conftest``) is actually DEFINED or
RE-EXPORTED there. The motivating dogfood finding: a generated suite imported 8+
helper symbols nothing defined → pytest aborted at COLLECTION (exit 2). The gate
runs BEFORE pytest and FAILS HONESTLY with a precise diagnosis instead.

These tests cover :func:`codd.test_import_coherence.check_test_import_coherence`:

* a coherent suite (helpers defined / ``__init__`` re-exports exactly what tests
  import) PASSES — no false-RED;
* an incoherent import (a name no sibling / helper ``__init__`` / conftest
  defines) FAILS with a precise, per-symbol diagnosis;
* conservative non-flagging: an unresolved ``import *`` chain or a dynamic
  ``__all__`` is NOT flagged (anti-false-RED);
* the gate is SCOPED to the test tree — it never re-flags source-package imports
  that the source/test gate already governs;
* the explicit opt-out is honored and the gate is never weakened silently.
"""

from __future__ import annotations

from pathlib import Path

from codd.test_import_coherence import check_test_import_coherence


def _base(tmp_path: Path, name: str = "todo-cli") -> Path:
    """A minimal src-layout package + an importable test package."""
    pkg = tmp_path / "src" / name.replace("-", "_")
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def add(x):\n    return x\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    return tmp_path


def _check(tmp_path: Path, name: str = "todo-cli", config=None):
    return check_test_import_coherence(
        tmp_path,
        language="python",
        project_name=name,
        source_dirs=["src/"],
        test_dirs=["tests/"],
        config=config,
    )


def _symbols(result) -> list[str]:
    return sorted(f.details["symbol"] for f in result.findings)


# ═══════════════════════════════════════════════════════════
# coherent suites pass (no false-RED)
# ═══════════════════════════════════════════════════════════


class TestCoherentPasses:
    def test_sibling_helper_module_defines_all_imported(self, tmp_path):
        _base(tmp_path)
        (tmp_path / "tests" / "helpers.py").write_text(
            "def run_cli(args):\n    return 0\n\n\ndef combined_output(r):\n    return ''\n"
        )
        (tmp_path / "tests" / "test_x.py").write_text(
            "from helpers import run_cli, combined_output\n\n\n"
            "def test_a():\n    assert run_cli([]) == 0\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()
        assert result.findings == []

    def test_helper_package_init_reexports_exactly(self, tmp_path):
        _base(tmp_path)
        helpers = tmp_path / "tests" / "helpers"
        helpers.mkdir()
        (helpers / "__init__.py").write_text(
            "from .io import load_json, write_json\n__all__ = ['load_json', 'write_json']\n"
        )
        (helpers / "io.py").write_text(
            "def load_json(p):\n    return {}\n\n\ndef write_json(p, d):\n    return None\n"
        )
        (tmp_path / "tests" / "test_y.py").write_text(
            "from helpers import load_json, write_json\n\n\n"
            "def test_a():\n    assert load_json(1) == {}\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()

    def test_aliased_reexport_is_provided(self, tmp_path):
        _base(tmp_path)
        helpers = tmp_path / "tests" / "helpers"
        helpers.mkdir()
        (helpers / "__init__.py").write_text(
            "from .a import alpha\nfrom .b import beta as B\n"
        )
        (helpers / "a.py").write_text("def alpha():\n    return 1\n")
        (helpers / "b.py").write_text("def beta():\n    return 2\n")
        (tmp_path / "tests" / "test_re.py").write_text(
            "from helpers import alpha, B\n\n\ndef test_a():\n    assert alpha() == 1 and B() == 2\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()

    def test_test_root_prefixed_dialect_resolves(self, tmp_path):
        # `from tests.helpers import x` (test-root-prefixed) resolves too.
        _base(tmp_path)
        (tmp_path / "tests" / "helpers.py").write_text("def helper():\n    return 7\n")
        (tmp_path / "tests" / "test_p.py").write_text(
            "from tests.helpers import helper\n\n\ndef test_a():\n    assert helper() == 7\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()

    def test_relative_import_within_test_subpackage_present(self, tmp_path):
        _base(tmp_path)
        sub = tmp_path / "tests" / "sub"
        sub.mkdir()
        (sub / "__init__.py").write_text("")
        (sub / "util.py").write_text("def helper():\n    return 1\n")
        (sub / "test_r.py").write_text(
            "from .util import helper\n\n\ndef test_a():\n    assert helper() == 1\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()

    def test_no_profile_stack_is_passing_noop(self, tmp_path):
        result = check_test_import_coherence(tmp_path, language="rust", project_name="x")
        assert result.passed
        assert "no layout profile" in result.detail

    def test_no_test_root_is_passing_noop(self, tmp_path):
        # src exists, but no tests/ dir → nothing to check.
        pkg = tmp_path / "src" / "todo_cli"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        result = _check(tmp_path)
        assert result.passed
        assert "no test root" in result.detail


# ═══════════════════════════════════════════════════════════
# incoherent imports fail honestly (the core finding)
# ═══════════════════════════════════════════════════════════


class TestIncoherentFails:
    def test_sibling_module_missing_symbol_fails(self, tmp_path):
        _base(tmp_path)
        (tmp_path / "tests" / "helpers.py").write_text("def run_cli(args):\n    return 0\n")
        (tmp_path / "tests" / "test_x.py").write_text(
            "from helpers import run_cli, combined_output, load_todo_json\n\n\n"
            "def test_a():\n    assert run_cli([]) == 0\n"
        )
        result = _check(tmp_path)
        assert not result.passed
        # Only the truly-missing symbols are flagged — never the defined one.
        assert _symbols(result) == ["combined_output", "load_todo_json"]
        assert all(f.kind == "missing_test_helper_symbol" for f in result.findings)
        # Precise diagnosis: file, symbol, target module all named.
        msg = result.findings[0].message
        assert "combined_output" in msg and "helpers" in msg
        assert "test_x.py" in result.findings[0].path

    def test_helper_init_missing_reexport_fails(self, tmp_path):
        _base(tmp_path)
        helpers = tmp_path / "tests" / "helpers"
        helpers.mkdir()
        # __init__ re-exports only load_json; tests also import write_json.
        (helpers / "__init__.py").write_text("from .io import load_json\n")
        (helpers / "io.py").write_text(
            "def load_json(p):\n    return {}\n\n\ndef write_json(p, d):\n    return None\n"
        )
        (tmp_path / "tests" / "test_y.py").write_text(
            "from helpers import load_json, write_json\n\n\n"
            "def test_a():\n    assert load_json(1) == {}\n"
        )
        result = _check(tmp_path)
        assert not result.passed
        assert _symbols(result) == ["write_json"]

    def test_conftest_missing_symbol_fails(self, tmp_path):
        _base(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text(
            "def make_tmp_todo(p):\n    return p\n"
        )
        (tmp_path / "tests" / "test_c.py").write_text(
            "from conftest import make_tmp_todo, NOT_THERE\n\n\n"
            "def test_a():\n    assert make_tmp_todo(1) == 1\n"
        )
        result = _check(tmp_path)
        assert not result.passed
        assert _symbols(result) == ["NOT_THERE"]

    def test_relative_import_missing_symbol_fails(self, tmp_path):
        _base(tmp_path)
        sub = tmp_path / "tests" / "sub"
        sub.mkdir()
        (sub / "__init__.py").write_text("")
        (sub / "util.py").write_text("def helper():\n    return 1\n")
        (sub / "test_r.py").write_text(
            "from .util import helper, missing_one\n\n\n"
            "def test_a():\n    assert helper() == 1\n"
        )
        result = _check(tmp_path)
        assert not result.passed
        assert _symbols(result) == ["missing_one"]

    def test_resolvable_intree_star_chain_still_flags_missing(self, tmp_path):
        # __init__ does `from .io import *`; io defines load_json via __all__.
        # The chain is FULLY resolvable, so a missing symbol IS flagged (not a
        # false-RED): load_json passes, ghost_symbol fails.
        _base(tmp_path)
        helpers = tmp_path / "tests" / "helpers"
        helpers.mkdir()
        (helpers / "__init__.py").write_text("from .io import *\n")
        (helpers / "io.py").write_text(
            "def load_json(p):\n    return {}\n__all__ = ['load_json']\n"
        )
        (tmp_path / "tests" / "test_t.py").write_text(
            "from helpers import load_json, ghost_symbol\n\n\n"
            "def test_a():\n    assert load_json(1) == {}\n"
        )
        result = _check(tmp_path)
        assert not result.passed
        assert _symbols(result) == ["ghost_symbol"]

    def test_summary_directs_regenerate_not_stub(self, tmp_path):
        _base(tmp_path)
        (tmp_path / "tests" / "helpers.py").write_text("X = 1\n")
        (tmp_path / "tests" / "test_x.py").write_text(
            "from helpers import absent_symbol\n\n\ndef test_a():\n    assert absent_symbol\n"
        )
        result = _check(tmp_path)
        assert not result.passed
        summary = result.summary()
        assert "REGENERATE" in summary
        # Diagnose-only: never promises to auto-create the helper.
        assert "stubs are never auto-created" in summary


# ═══════════════════════════════════════════════════════════
# conservative non-flagging (anti-false-RED)
# ═══════════════════════════════════════════════════════════


class TestConservativeNonFlagging:
    def test_unresolved_star_chain_not_flagged(self, tmp_path):
        # helper re-exports via `from <third-party> import *` — the source is NOT
        # in the test tree, so its provided names are UNKNOWN → do not flag.
        _base(tmp_path)
        (tmp_path / "tests" / "helpers.py").write_text(
            "from some_thirdparty.dynmod import *\n"
        )
        (tmp_path / "tests" / "test_z.py").write_text(
            "from helpers import mystery_symbol\n\n\n"
            "def test_a():\n    assert mystery_symbol\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()

    def test_dynamic_dunder_all_not_flagged(self, tmp_path):
        # A computed __all__ makes the public surface undecidable → never flag.
        _base(tmp_path)
        (tmp_path / "tests" / "helpers.py").write_text(
            "import os\n__all__ = [n for n in dir(os)]\n"
        )
        (tmp_path / "tests" / "test_z2.py").write_text(
            "from helpers import anything_at_all\n\n\n"
            "def test_a():\n    assert anything_at_all\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()

    def test_unresolved_intree_star_makes_target_unknown(self, tmp_path):
        # __init__ does `from .gen import *` but gen's __all__ is dynamic → the
        # whole helper surface is UNKNOWN → imports off it are not flagged.
        _base(tmp_path)
        helpers = tmp_path / "tests" / "helpers"
        helpers.mkdir()
        (helpers / "__init__.py").write_text("from .gen import *\n")
        (helpers / "gen.py").write_text(
            "import string\n__all__ = list(string.ascii_letters)\n"
        )
        (tmp_path / "tests" / "test_u.py").write_text(
            "from helpers import whatever_symbol\n\n\n"
            "def test_a():\n    assert whatever_symbol\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()

    def test_import_star_in_the_test_itself_not_checked(self, tmp_path):
        # The IMPORTING test receiving `*` cannot be symbol-checked (it pulls
        # whatever the source has). No crash, no flag.
        _base(tmp_path)
        (tmp_path / "tests" / "helpers.py").write_text("def helper():\n    return 1\n")
        (tmp_path / "tests" / "test_star.py").write_text(
            "from helpers import *\n\n\ndef test_a():\n    assert helper() == 1\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()


# ═══════════════════════════════════════════════════════════
# scoped to the test tree (no overlap with source/test gate)
# ═══════════════════════════════════════════════════════════


class TestScopedToTestTree:
    def test_source_package_import_is_out_of_scope(self, tmp_path):
        # Importing from the SOURCE package (todo_cli.core) is the source/test
        # gate's job, not this one — never flagged here, even for a bogus symbol.
        _base(tmp_path)
        (tmp_path / "tests" / "test_src.py").write_text(
            "from todo_cli.core import add, nonexistent_src_symbol\n\n\n"
            "def test_a():\n    assert add(1) == 1\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()
        assert "0 in-test import site(s) checked" in result.detail

    def test_stdlib_and_thirdparty_imports_not_flagged(self, tmp_path):
        _base(tmp_path)
        (tmp_path / "tests" / "test_std.py").write_text(
            "import json\nfrom pathlib import Path\nimport pytest\n\n\n"
            "def test_a():\n    assert json and Path\n"
        )
        result = _check(tmp_path)
        assert result.passed, result.summary()

    def test_only_test_files_are_inspected_as_importers(self, tmp_path):
        # A helper module (non test_*) importing a missing symbol from another
        # helper is NOT inspected as an importer here — only pytest-collected
        # TEST modules are. (The honesty backstop is that a real test importing
        # that helper transitively would still surface a problem.)
        _base(tmp_path)
        (tmp_path / "tests" / "a.py").write_text("def alpha():\n    return 1\n")
        # b.py is a helper (not a test) importing a missing symbol from a.
        (tmp_path / "tests" / "b.py").write_text("from a import alpha, missing\n")
        result = _check(tmp_path)
        # No TEST file imports anything → nothing flagged.
        assert result.passed, result.summary()


# ═══════════════════════════════════════════════════════════
# opt-out (explicit, never weakened silently)
# ═══════════════════════════════════════════════════════════


class TestOptOut:
    def test_opt_out_disables_gate(self, tmp_path):
        _base(tmp_path)
        (tmp_path / "tests" / "helpers.py").write_text("X = 1\n")
        (tmp_path / "tests" / "test_x.py").write_text("from helpers import absent\n")
        result = _check(tmp_path, config={"coherence": {"import_coherence": False}})
        assert result.passed
        assert "disabled" in result.detail

    def test_default_is_on(self, tmp_path):
        _base(tmp_path)
        (tmp_path / "tests" / "helpers.py").write_text("X = 1\n")
        (tmp_path / "tests" / "test_x.py").write_text("from helpers import absent\n")
        result = _check(tmp_path, config={})
        assert not result.passed


# ═══════════════════════════════════════════════════════════
# Path-escape jail — scan.test_dirs is user-controllable (codd.yaml).
# A ``../`` traversal or an in-root symlink whose target escapes the
# tree must NEVER walk/read test files OUTSIDE the project root.
# ═══════════════════════════════════════════════════════════


class TestPathEscapeJail:
    def _outside_test_tree(self, tmp_path: Path) -> Path:
        """A SIBLING dir holding a 'test' that, if read, would flag a missing symbol."""
        outside = tmp_path.parent / (tmp_path.name + "_outside")
        outside.mkdir()
        (outside / "__init__.py").write_text("")
        (outside / "helpers.py").write_text("X = 1\n")
        # if read as a project test, this would flag 'absent_symbol' (missing).
        (outside / "test_evil.py").write_text("from helpers import absent_symbol\n")
        return outside

    def test_parent_traversal_test_dir_is_not_read(self, tmp_path):
        _base(tmp_path)
        outside = self._outside_test_tree(tmp_path)
        result = check_test_import_coherence(
            tmp_path,
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=[f"../{outside.name}"],  # escapes the project root
        )
        assert not any("test_evil" in f.path for f in result.findings), result.summary()

    def test_symlinked_test_dir_escaping_root_is_not_read(self, tmp_path):
        _base(tmp_path)
        outside = self._outside_test_tree(tmp_path)
        link = tmp_path / "linked_tests"
        link.symlink_to(outside, target_is_directory=True)
        result = check_test_import_coherence(
            tmp_path,
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=["linked_tests"],
        )
        assert not any("test_evil" in f.path for f in result.findings), result.summary()

    def test_symlinked_test_file_inside_tree_escaping_root_is_dropped(self, tmp_path):
        # An in-root test tree may hold a symlink FILE whose target escapes the
        # root; the re-confinement must drop it (not read it as a project test).
        _base(tmp_path)
        outside = self._outside_test_tree(tmp_path)
        (tmp_path / "tests" / "test_leak.py").symlink_to(outside / "test_evil.py")
        result = _check(tmp_path)
        assert not any("test_leak" in f.path for f in result.findings), result.summary()

    def test_in_root_layout_unchanged(self, tmp_path):
        # ANTI-FALSE-RED: a genuine in-root missing-symbol violation is still flagged.
        _base(tmp_path)
        (tmp_path / "tests" / "helpers.py").write_text("def present():\n    return 1\n")
        (tmp_path / "tests" / "test_x.py").write_text(
            "from helpers import present, gone\n\n\ndef test_a():\n    assert present() == 1\n"
        )
        result = _check(tmp_path)
        assert not result.passed
        assert _symbols(result) == ["gone"]
