"""Tests for the e2e-contract (no-runtime-import) coherence gate (A-core anti-false-green).

A sibling to ``test_import_coherence`` / ``test_test_import_coherence`` on the
e2e-import-CONTRACT axis. For a CLI / subprocess e2e modality, the e2e tests AND
their shared e2e helpers must invoke the built/installed entrypoint as a
SUBPROCESS and must NOT import the application/runtime (source) package. The
motivating dogfood finding: a generated e2e helper had a function-scoped
``from todo_cli import cli`` — violating the project's OWN generated governance
test (``"todo_cli" not in imported_roots(path)`` over ``tests/e2e/**``) — so verify
failed at a RUN-PHASE assertion that auto-repair (correctly) refused to touch. The
gate runs BEFORE pytest and FAILS HONESTLY with a precise diagnosis instead.

These tests cover :func:`codd.e2e_contract_coherence.check_e2e_contract_coherence`:

* a compliant subprocess-only e2e suite PASSES — no false-RED;
* an e2e test OR helper importing the runtime package (module-level AND
  function-scoped) FAILS with a precise diagnosis (cli modality);
* a browser/device modality with a runtime/client import is NOT flagged
  (modality carve-out — anti-false-RED);
* an untyped project (undecidable modality) is NOT flagged (anti-false-RED);
* a runtime-importing helper in the UNIT tree (outside ``tests/e2e``) is NOT
  flagged (e2e-tree scope);
* the explicit opt-out is honored and the gate is never weakened silently.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.e2e_contract_coherence import check_e2e_contract_coherence


PKG = "todo_cli"


def _base(tmp_path: Path, name: str = "todo-cli") -> Path:
    """A minimal src-layout package + an importable test package with an e2e tree."""
    pkg = tmp_path / "src" / name.replace("-", "_")
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "cli.py").write_text("def cli(argv):\n    return 0\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    e2e = tests / "e2e"
    e2e.mkdir()
    (e2e / "__init__.py").write_text("")
    return tmp_path


def _check(tmp_path: Path, name: str = "todo-cli", config=None):
    return check_e2e_contract_coherence(
        tmp_path,
        language="python",
        project_name=name,
        source_dirs=["src/"],
        test_dirs=["tests/"],
        config=config,
    )


def _cli_config() -> dict:
    """A config whose project type resolves to the ``cli`` e2e modality."""
    return {"required_artifacts": {"project_type": "cli"}}


def _roots(result) -> list[str]:
    return sorted(f.details["import_root"] for f in result.findings)


# ═══════════════════════════════════════════════════════════
# compliant subprocess-only e2e suites pass (no false-RED)
# ═══════════════════════════════════════════════════════════


class TestCompliantPasses:
    def test_subprocess_only_e2e_passes(self, tmp_path):
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        helpers = e2e / "helpers"
        helpers.mkdir()
        (helpers / "__init__.py").write_text("")
        (helpers / "cli.py").write_text(
            "import subprocess\n\n\n"
            "def invoke_cli(argv):\n"
            "    return subprocess.run(['todo'] + argv, capture_output=True)\n"
        )
        (e2e / "test_flow.py").write_text(
            "from .helpers.cli import invoke_cli\n\n\n"
            "def test_run():\n    assert invoke_cli([]).returncode == 0\n"
        )
        result = _check(tmp_path, config=_cli_config())
        assert result.passed, result.summary()
        assert result.findings == []

    def test_relative_intra_e2e_import_not_flagged(self, tmp_path):
        # A relative import addresses the e2e package itself, never the runtime.
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "support.py").write_text("def fixture():\n    return 1\n")
        (e2e / "test_x.py").write_text(
            "from . import support\nimport subprocess\n\n\n"
            "def test_a():\n    assert support.fixture() == 1\n"
        )
        result = _check(tmp_path, config=_cli_config())
        assert result.passed, result.summary()

    def test_e2e_importing_thirdparty_not_flagged(self, tmp_path):
        # Importing a non-runtime package (stdlib / third party) is fine.
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "test_x.py").write_text(
            "import subprocess\nimport json\n\n\n"
            "def test_a():\n    assert subprocess.run(['todo']).returncode == 0\n"
        )
        result = _check(tmp_path, config=_cli_config())
        assert result.passed, result.summary()


# ═══════════════════════════════════════════════════════════
# runtime imports under the e2e tree FAIL (cli modality)
# ═══════════════════════════════════════════════════════════


class TestRuntimeImportFails:
    def test_module_level_runtime_import_in_helper_fails(self, tmp_path):
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        helpers = e2e / "helpers"
        helpers.mkdir()
        (helpers / "__init__.py").write_text("")
        (helpers / "cli.py").write_text(
            f"from {PKG} import cli\n\n\n"
            "def invoke_cli_unit(argv):\n    return cli(argv)\n"
        )
        result = _check(tmp_path, config=_cli_config())
        assert not result.passed
        assert _roots(result) == [PKG]
        finding = result.findings[0]
        assert finding.kind == "e2e_runtime_import"
        assert finding.path.endswith("tests/e2e/helpers/cli.py")
        assert finding.details["scoped"] is False
        assert "no-runtime-import" in finding.message
        assert "REGENERATE" in result.summary()

    def test_function_scoped_runtime_import_in_helper_fails(self, tmp_path):
        # The EXACT dogfood shape: a function-scoped runtime import in a helper.
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        helpers = e2e / "helpers"
        helpers.mkdir()
        (helpers / "__init__.py").write_text("")
        (helpers / "cli.py").write_text(
            "def invoke_cli_unit(argv):\n"
            f"    from {PKG} import cli\n"
            "    return cli(argv)\n"
        )
        result = _check(tmp_path, config=_cli_config())
        assert not result.passed
        assert _roots(result) == [PKG]
        finding = result.findings[0]
        assert finding.details["scoped"] is True
        assert "function-scoped" in finding.message

    def test_runtime_import_in_e2e_test_file_fails(self, tmp_path):
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "test_flow.py").write_text(
            f"import {PKG}.cli\n\n\n"
            "def test_run():\n    assert True\n"
        )
        result = _check(tmp_path, config=_cli_config())
        assert not result.passed
        assert _roots(result) == [PKG]

    def test_dotted_from_runtime_import_flags_root(self, tmp_path):
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "test_flow.py").write_text(
            f"from {PKG}.cli import cli\n\n\n"
            "def test_run():\n    assert True\n"
        )
        result = _check(tmp_path, config=_cli_config())
        assert not result.passed
        assert _roots(result) == [PKG]


# ═══════════════════════════════════════════════════════════
# modality carve-out (anti-false-RED)
# ═══════════════════════════════════════════════════════════


class TestModalityCarveOut:
    def test_browser_modality_not_flagged(self, tmp_path):
        # A browser e2e suite legitimately imports a client/runtime → no-op.
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "test_flow.py").write_text(
            f"from {PKG} import cli\n\n\n"
            "def test_run():\n    assert True\n"
        )
        result = _check(tmp_path, config={"required_artifacts": {"project_type": "web"}})
        assert result.passed, result.summary()
        assert "skipped" in result.detail

    def test_device_modality_not_flagged(self, tmp_path):
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "test_flow.py").write_text(
            f"from {PKG} import cli\n\n\n"
            "def test_run():\n    assert True\n"
        )
        result = _check(tmp_path, config={"required_artifacts": {"project_type": "mobile"}})
        assert result.passed, result.summary()

    def test_untyped_project_not_flagged(self, tmp_path):
        # Undecidable modality → no-op (anti-false-RED), even with a runtime import.
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "test_flow.py").write_text(
            f"from {PKG} import cli\n\n\n"
            "def test_run():\n    assert True\n"
        )
        result = _check(tmp_path, config=None)
        assert result.passed, result.summary()

    def test_explicit_no_runtime_import_contract_activates(self, tmp_path):
        # An explicit declaration activates the gate even without a typed profile.
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "test_flow.py").write_text(
            f"from {PKG} import cli\n\n\n"
            "def test_run():\n    assert True\n"
        )
        result = _check(
            tmp_path, config={"coherence": {"e2e_import_contract": "no_runtime_import"}}
        )
        assert not result.passed
        assert _roots(result) == [PKG]

    def test_explicit_allow_runtime_import_deactivates(self, tmp_path):
        # An explicit allow overrides even a cli-typed project (author's call).
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "test_flow.py").write_text(
            f"from {PKG} import cli\n\n\n"
            "def test_run():\n    assert True\n"
        )
        config = _cli_config()
        config["coherence"] = {"e2e_import_contract": "allow_runtime_import"}
        result = _check(tmp_path, config=config)
        assert result.passed, result.summary()


# ═══════════════════════════════════════════════════════════
# scope: only the e2e tree (anti-false-RED)
# ═══════════════════════════════════════════════════════════


class TestE2ETreeScope:
    def test_runtime_importing_unit_helper_not_flagged(self, tmp_path):
        # A function-scoped in-process helper in the UNIT tree (outside e2e) is fine.
        _base(tmp_path)
        unit_helpers = tmp_path / "tests" / "helpers.py"
        unit_helpers.write_text(
            "def call_direct(argv):\n"
            f"    from {PKG} import cli\n"
            "    return cli(argv)\n"
        )
        (tmp_path / "tests" / "test_unit.py").write_text(
            "from helpers import call_direct\n\n\n"
            "def test_a():\n    assert call_direct([]) == 0\n"
        )
        result = _check(tmp_path, config=_cli_config())
        assert result.passed, result.summary()

    def test_no_e2e_tree_is_noop(self, tmp_path):
        # No tests/e2e directory at all → passing no-op.
        pkg = tmp_path / "src" / PKG
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "cli.py").write_text("def cli(argv):\n    return 0\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("")
        (tests / "test_unit.py").write_text(f"import {PKG}.cli\n\n\ndef test_a():\n    assert True\n")
        result = _check(tmp_path, config=_cli_config())
        assert result.passed, result.summary()
        assert "no e2e tree" in result.detail


# ═══════════════════════════════════════════════════════════
# undecidable source identity (anti-false-RED) + opt-out
# ═══════════════════════════════════════════════════════════


class TestConservativeNonFlagging:
    def test_undecidable_source_identity_not_flagged(self, tmp_path):
        # An empty/garbage project name yields the ``app`` fallback package, but a
        # name that normalizes to a non-identifier leaves source identity
        # undecidable — the gate then does not flag. We force this by pointing the
        # gate at a profile whose package_name is not a valid identifier via an
        # explicitly-passed profile.
        from codd.project_types import LayoutProfile

        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "test_flow.py").write_text(
            f"from {PKG} import cli\n\n\ndef test_run():\n    assert True\n"
        )
        bad_profile = LayoutProfile(
            language="python",
            package_name="",  # undecidable identity
            source_root="src",
            package_root="src",
            test_root="tests",
        )
        result = check_e2e_contract_coherence(
            tmp_path,
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=["tests/"],
            config=_cli_config(),
            profile=bad_profile,
        )
        assert result.passed, result.summary()
        assert "undecidable" in result.detail

    def test_opt_out_disables_gate(self, tmp_path):
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "test_flow.py").write_text(
            f"from {PKG} import cli\n\n\ndef test_run():\n    assert True\n"
        )
        config = _cli_config()
        config["coherence"] = {"import_coherence": False}
        result = _check(tmp_path, config=config)
        assert result.passed, result.summary()
        assert "disabled" in result.detail

    def test_no_layout_profile_is_noop(self, tmp_path):
        # A stack without a layout profile is a passing no-op.
        result = check_e2e_contract_coherence(
            tmp_path,
            language="ruby",  # no layout profile
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=["tests/"],
            config=_cli_config(),
        )
        assert result.passed
        assert "no layout profile" in result.detail

    def test_unparseable_e2e_file_not_flagged(self, tmp_path):
        # A syntactically-broken e2e file is skipped (the syntax gate's job), not
        # crashed on — and other files are still scanned.
        _base(tmp_path)
        e2e = tmp_path / "tests" / "e2e"
        (e2e / "broken.py").write_text("def (:\n")  # syntax error
        (e2e / "test_ok.py").write_text("import subprocess\n\n\ndef test_a():\n    assert True\n")
        result = _check(tmp_path, config=_cli_config())
        assert result.passed, result.summary()

    def test_typescript_e2e_tree_is_a_clean_noop(self, tmp_path):
        # SCOPE: this gate is a Python-AST contract gate. A TypeScript project
        # with a ``.e2e.ts`` e2e tree (even one that imports the runtime) is a
        # clean PASSING no-op — the Python ``*.py`` scan finds nothing, so the
        # gate neither crashes nor false-flags. TS import-contract analysis is a
        # separate concern, intentionally NOT introduced by .e2e.ts suffix
        # recognition (which only governs the VB scan + vitest run discovery).
        e2e = tmp_path / "tests" / "e2e"
        e2e.mkdir(parents=True)
        (e2e / "tempconv.e2e.ts").write_text(
            "import { convert } from '../../src/index';\nimport 'vitest';\n"
        )
        result = check_e2e_contract_coherence(
            tmp_path,
            language="typescript",
            project_name="tempconv",
            source_dirs=["src/"],
            test_dirs=["tests/"],
            config=_cli_config(),
        )
        assert result.passed
        assert result.findings == []


# ═══════════════════════════════════════════════════════════
# Path-escape jail — scan.test_dirs is user-controllable (codd.yaml).
# A ``../`` traversal or an in-root symlink whose target escapes the
# tree must NEVER scan e2e files OUTSIDE the project root (no out-of-root
# file read as e2e content; escape must not crash and must not false-flag).
# ═══════════════════════════════════════════════════════════


class TestPathEscapeJail:
    def _outside_e2e_tree(self, tmp_path: Path) -> Path:
        """A SIBLING ``<x>/e2e`` tree whose file, if read, would flag a runtime import."""
        outside = tmp_path.parent / (tmp_path.name + "_outside")
        e2e = outside / "e2e"
        e2e.mkdir(parents=True)
        (e2e / "__init__.py").write_text("")
        (e2e / "test_evil.py").write_text(f"from {PKG} import cli\n")
        return outside

    def test_parent_traversal_test_dir_is_not_scanned(self, tmp_path):
        _base(tmp_path)
        outside = self._outside_e2e_tree(tmp_path)
        result = check_e2e_contract_coherence(
            tmp_path,
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=[f"../{outside.name}"],  # escapes the project root
            config=_cli_config(),
        )
        assert not any("test_evil" in f.path for f in result.findings), result.summary()

    def test_symlinked_test_dir_escaping_root_is_not_scanned(self, tmp_path):
        # An in-root test_root that is a symlink whose target escapes the tree
        # must not let the gate scan the off-root <test_root>/e2e for runtime imports.
        _base(tmp_path)
        outside = self._outside_e2e_tree(tmp_path)
        link = tmp_path / "linked_tests"
        link.symlink_to(outside, target_is_directory=True)
        result = check_e2e_contract_coherence(
            tmp_path,
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=["linked_tests"],
            config=_cli_config(),
        )
        assert not any("test_evil" in f.path for f in result.findings), result.summary()

    def test_symlinked_e2e_file_inside_tree_escaping_root_is_dropped(self, tmp_path):
        # An in-root e2e tree may hold a symlink FILE whose target escapes the
        # root; re-confinement must drop it (not scan it as an e2e file).
        _base(tmp_path)
        outside = self._outside_e2e_tree(tmp_path)
        (tmp_path / "tests" / "e2e" / "test_leak.py").symlink_to(
            outside / "e2e" / "test_evil.py"
        )
        result = _check(tmp_path, config=_cli_config())
        assert not any("test_leak" in f.path for f in result.findings), result.summary()

    def test_in_root_e2e_tree_unchanged(self, tmp_path):
        # ANTI-FALSE-RED: a genuine in-root runtime import is still flagged.
        _base(tmp_path)
        (tmp_path / "tests" / "e2e" / "test_flow.py").write_text(
            f"from {PKG} import cli\n\n\ndef test_run():\n    assert True\n"
        )
        result = _check(tmp_path, config=_cli_config())
        assert not result.passed
        assert _roots(result) == [PKG]
