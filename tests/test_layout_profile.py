"""Tests for the harness-owned layout profile + deterministic scaffold (A-core).

The greenfield autopilot must OWN the repository topology and module resolution
so cross-vendor builds produce coherent source + tests. These tests cover the
:class:`~codd.project_types.LayoutProfile` registry and
:func:`~codd.project_types.scaffold_layout`:

* the Python profile derives package_name from the project name and roots from
  ``scan.*_dirs`` (no hardcoded literals);
* the scaffold creates the topology IDEMPOTENTLY and does NOT clobber existing
  valid files (so a coherent Claude layout / a --resume is a no-op);
* the emitted pyproject runs tests against the real package (NO pythonpath ".").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.project_types import (
    LayoutProfile,
    normalize_package_name,
    resolve_layout_profile,
    scaffold_layout,
    supported_layout_profile_languages,
)


# ═══════════════════════════════════════════════════════════
# package-name normalization (deterministic, valid identifier)
# ═══════════════════════════════════════════════════════════


class TestNormalizePackageName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("todo-cli", "todo_cli"),
            ("Todo CLI", "todo_cli"),
            ("my.cool.app", "my_cool_app"),
            ("2048-game", "_2048_game"),  # cannot start with a digit
            ("  spaced  name ", "spaced_name"),
            ("already_ok", "already_ok"),
            ("--weird--", "weird"),
        ],
    )
    def test_derives_valid_identifier(self, raw, expected):
        result = normalize_package_name(raw)
        assert result == expected
        assert result.isidentifier()

    def test_empty_falls_back(self):
        assert normalize_package_name("") == "app"
        assert normalize_package_name(None) == "app"
        assert normalize_package_name("---") == "app"


# ═══════════════════════════════════════════════════════════
# profile resolution (registry, derived paths, no literals)
# ═══════════════════════════════════════════════════════════


class TestResolveLayoutProfile:
    def test_python_profile_resolves_with_derived_paths(self):
        profile = resolve_layout_profile(
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=["tests/"],
        )
        assert isinstance(profile, LayoutProfile)
        assert profile.language == "python"
        assert profile.package_name == "todo_cli"
        assert profile.source_root == "src"
        assert profile.package_root == "src/todo_cli"
        assert profile.test_root == "tests"
        assert profile.runner == "pytest"
        assert profile.install_mode == "editable"
        assert profile.test_import_policy == "package_absolute"

    def test_paths_derive_from_scan_dirs_not_literals(self):
        profile = resolve_layout_profile(
            language="python",
            project_name="my-app",
            source_dirs=["app/"],
            test_dirs=["spec/"],
        )
        assert profile.source_root == "app"
        assert profile.package_root == "app/my_app"
        assert profile.test_root == "spec"

    def test_defaults_when_scan_dirs_absent(self):
        profile = resolve_layout_profile(
            language="python", project_name="demo", source_dirs=None, test_dirs=None
        )
        assert profile.source_root == "src"
        assert profile.test_root == "tests"
        assert profile.package_root == "src/demo"

    def test_unknown_language_has_no_profile(self):
        assert resolve_layout_profile(language="rust", project_name="x") is None
        assert resolve_layout_profile(language=None, project_name="x") is None

    def test_python_is_registered(self):
        assert "python" in supported_layout_profile_languages()


# ═══════════════════════════════════════════════════════════
# deterministic scaffold (idempotent, non-clobbering)
# ═══════════════════════════════════════════════════════════


class TestScaffoldLayout:
    def _profile(self, name="todo-cli"):
        return resolve_layout_profile(
            language="python", project_name=name, source_dirs=["src/"], test_dirs=["tests/"]
        )

    def test_creates_full_topology(self, tmp_path):
        profile = self._profile()
        result = scaffold_layout(tmp_path, profile)

        assert (tmp_path / "src" / "todo_cli" / "__init__.py").is_file()
        assert (tmp_path / "src" / "todo_cli" / "__main__.py").is_file()
        assert (tmp_path / "tests" / "__init__.py").is_file()
        assert (tmp_path / "pyproject.toml").is_file()
        # the pyproject runs tests against the real package — NO "." hole.
        text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert 'pythonpath = ["src"]' in text
        assert '"."' not in text
        assert "--import-mode=importlib" in text
        assert "src/todo_cli/__init__.py" in result.created

    def test_idempotent_second_call_creates_nothing(self, tmp_path):
        profile = self._profile()
        scaffold_layout(tmp_path, profile)
        snapshot = {
            p.relative_to(tmp_path).as_posix(): p.read_text(encoding="utf-8")
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        result = scaffold_layout(tmp_path, profile)
        assert result.created == ()  # nothing new
        after = {
            p.relative_to(tmp_path).as_posix(): p.read_text(encoding="utf-8")
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        assert after == snapshot  # byte-for-byte unchanged

    def test_does_not_clobber_existing_package_files(self, tmp_path):
        # A coherent Claude layout already on disk must be left untouched.
        profile = self._profile()
        pkg = tmp_path / "src" / "todo_cli"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('"""authored."""\n', encoding="utf-8")
        (pkg / "__main__.py").write_text("# authored main\n", encoding="utf-8")

        result = scaffold_layout(tmp_path, profile)

        assert (pkg / "__init__.py").read_text(encoding="utf-8") == '"""authored."""\n'
        assert (pkg / "__main__.py").read_text(encoding="utf-8") == "# authored main\n"
        assert "src/todo_cli/__init__.py" in result.skipped

    def test_does_not_clobber_existing_strong_pyproject(self, tmp_path):
        original = '[tool.pytest.ini_options]\naddopts = "-x"\ntestpaths = ["tests"]\n'
        (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8")
        profile = self._profile()

        scaffold_layout(tmp_path, profile)

        # an author/AI pytest config is authoritative — left byte-for-byte.
        assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == original

    def test_unknown_stack_is_noop(self, tmp_path):
        profile = LayoutProfile(
            language="rust",
            package_name="x",
            source_root="src",
            package_root="src/x",
            test_root="tests",
        )
        result = scaffold_layout(tmp_path, profile)
        assert result.created == ()
        assert list(tmp_path.iterdir()) == []
