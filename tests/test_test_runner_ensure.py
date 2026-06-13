"""Tests for the stack-general test-runner-config ensurer.

The greenfield autopilot must DETERMINISTICALLY guarantee that, for a known
stack, the verify stage can RUN the generated tests — independent of whether the
generating AI happened to emit a runnable test config. These tests cover the
``codd.project_types.ensure_test_runner_config`` registry entry point:

* a Python project with NO detectable test config gets a pyproject ensured, and
  ``detect_test_command`` then resolves pytest (the core "runnable" guarantee);
* an existing/AI/user-provided config is NEVER clobbered (idempotent ensure);
* paths derive from the project's configured source/test dirs, not literals;
* unknown stacks are an advisory no-op (the verify honesty gate still applies);
* the ensured config makes the runner DETECTABLE only — it does not relax
  verification (anti-false-green): a failing/empty suite still fails honestly.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codd.project_types import (
    EnsureTestRunnerResult,
    ensure_test_runner_config,
    supported_test_runner_languages,
)
from codd.test_detection import detect_test_command


# ═══════════════════════════════════════════════════════════
# Core guarantee: a detectable, runnable pytest setup is created
# ═══════════════════════════════════════════════════════════


class TestPythonEnsureCreatesDetectableRunner:
    def test_no_config_gets_pyproject_and_detect_resolves_pytest(self, tmp_path):
        # Pre-condition: the live greenfield gap — tests exist but nothing is
        # detectable, so verify would "execute nothing".
        assert detect_test_command(tmp_path) is None

        result = ensure_test_runner_config(
            tmp_path, language="python", source_dirs=["src/"], test_dirs=["tests/"]
        )

        assert result.action == "created"
        pyproject = tmp_path / "pyproject.toml"
        assert pyproject.is_file()
        # detection now resolves pytest — the whole point of the fix.
        assert detect_test_command(tmp_path) == "pytest --tb=short -q"

    def test_testpaths_and_pythonpath_derive_from_scan_dirs(self, tmp_path):
        # No hardcoded "src"/"tests": the FIRST configured source root flows
        # through as the (sole) pythonpath. A-core: NO "." (false-green hole).
        ensure_test_runner_config(
            tmp_path,
            language="python",
            project_name="my-app",
            source_dirs=["app/", "lib/"],
            test_dirs=["spec/"],
        )
        text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert "[tool.pytest.ini_options]" in text
        assert 'testpaths = ["spec"]' in text
        # pythonpath = the source root ONLY (the package lives under app/my_app);
        # NO "." — a bare flat import must not resolve.
        assert 'pythonpath = ["app"]' in text
        assert '"."' not in text
        # importlib mode: no sys.path[0] insertion of the test dir.
        assert "--import-mode=importlib" in text

    def test_emitted_pyproject_is_valid_toml_and_has_no_dot_pythonpath(self, tmp_path):
        ensure_test_runner_config(
            tmp_path, language="python", project_name="demo", source_dirs=["src/"], test_dirs=["tests/"]
        )
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - py<3.11
            import tomli as tomllib  # type: ignore[no-redef]
        parsed = tomllib.loads((tmp_path / "pyproject.toml").read_text(encoding="utf-8"))
        ini = parsed["tool"]["pytest"]["ini_options"]
        assert ini["testpaths"] == ["tests"]
        # A-core anti-false-green: source root only, never ".".
        assert ini["pythonpath"] == ["src"]
        assert "." not in ini["pythonpath"]
        # editable package metadata is present so `pip install -e .` resolves it.
        assert parsed["project"]["name"] == "demo"
        assert parsed["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]

    def test_package_layout_importable_and_bare_basename_fails(self, tmp_path):
        # The harness-owned package layout: a package-absolute import RESOLVES,
        # and a bare-basename import does NOT (no "." on pythonpath). This is the
        # whole anti-false-green point of A-core.
        from codd.project_types import resolve_layout_profile, scaffold_layout

        profile = resolve_layout_profile(
            language="python", project_name="demo-pkg", source_dirs=["src/"], test_dirs=["tests/"]
        )
        # write a real module INTO the package root the profile owns.
        pkg_dir = tmp_path / profile.package_root
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "mymod.py").write_text("def add(a, b):\n    return a + b\n")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        # coherent (package-absolute) test:
        (tests_dir / "test_ok.py").write_text(
            f"from {profile.package_name}.mymod import add\n\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n"
        )
        scaffold_layout(tmp_path, profile)

        command = detect_test_command(tmp_path)
        assert command == "pytest --tb=short -q"
        proc = subprocess.run(
            [sys.executable, "-m", *command.split()],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "1 passed" in proc.stdout

        # Now a BARE-BASENAME import of the same module must NOT resolve.
        (tests_dir / "test_bare.py").write_text(
            "import importlib\n\n\n"
            "def test_bare():\n    importlib.import_module('mymod')\n"
        )
        proc2 = subprocess.run(
            [sys.executable, "-m", *command.split(), "tests/test_bare.py"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert proc2.returncode != 0, proc2.stdout + proc2.stderr
        assert "No module named 'mymod'" in (proc2.stdout + proc2.stderr)


# ═══════════════════════════════════════════════════════════
# Non-clobber / idempotence: never overwrite a provided config
# ═══════════════════════════════════════════════════════════


class TestNonClobber:
    def test_existing_pytest_pyproject_is_left_untouched(self, tmp_path):
        original = "[tool.pytest.ini_options]\naddopts = \"-x\"\ntestpaths = [\"mine\"]\n"
        (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8")

        result = ensure_test_runner_config(
            tmp_path, language="python", source_dirs=["src/"], test_dirs=["tests/"]
        )

        assert result.action == "present"
        # byte-for-byte unchanged — an AI/user config is authoritative.
        assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == original

    def test_bare_pyproject_is_augmented_for_runnability_not_replaced(self, tmp_path):
        # A BARE pyproject is detectable as pytest (rule 5) but NOT runnable for
        # a src layout — no pythonpath. The ensurer must UPGRADE it (append the
        # pytest section with pythonpath) while preserving the existing tables.
        original = '[project]\nname = "demo"\nversion = "0.1.0"\n'
        (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8")

        result = ensure_test_runner_config(
            tmp_path, language="python", project_name="demo", source_dirs=["src/"], test_dirs=["tests/"]
        )

        assert result.action == "augmented"
        text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        # the original [project] table is preserved; the pytest section is added.
        assert original.strip() in text
        assert "[tool.pytest.ini_options]" in text
        # A-core: source root only, no "." (the existing [project] is kept, so no
        # duplicate metadata is injected).
        assert 'pythonpath = ["src"]' in text
        assert '"."' not in text
        assert text.count("[project]") == 1
        assert detect_test_command(tmp_path) == "pytest --tb=short -q"

    def test_idempotent_second_call_is_a_noop(self, tmp_path):
        first = ensure_test_runner_config(
            tmp_path, language="python", source_dirs=["src/"], test_dirs=["tests/"]
        )
        assert first.action == "created"
        snapshot = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")

        second = ensure_test_runner_config(
            tmp_path, language="python", source_dirs=["src/"], test_dirs=["tests/"]
        )
        assert second.action == "present"
        assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == snapshot

    def test_existing_non_pytest_runner_is_respected(self, tmp_path):
        # A Python project whose author chose a Makefile test runner must NOT be
        # force-converted to pytest: a different, already-detectable command wins.
        (tmp_path / "Makefile").write_text("test:\n\t./run.sh\n", encoding="utf-8")

        result = ensure_test_runner_config(
            tmp_path, language="python", source_dirs=["src/"], test_dirs=["tests/"]
        )

        assert result.action == "present"
        assert not (tmp_path / "pyproject.toml").exists()
        # detection still resolves the author's chosen runner.
        assert detect_test_command(tmp_path) == "make test"

    def test_existing_pytest_ini_blocks_scaffold(self, tmp_path):
        # A strong pytest config in pytest.ini (not pyproject) is also respected.
        (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -q\n", encoding="utf-8")

        result = ensure_test_runner_config(
            tmp_path, language="python", source_dirs=["src/"], test_dirs=["tests/"]
        )

        assert result.action == "present"
        assert not (tmp_path / "pyproject.toml").exists()


# ═══════════════════════════════════════════════════════════
# Stack generality: registry, unknown stacks, defaults
# ═══════════════════════════════════════════════════════════


class TestStackGenerality:
    def test_python_is_registered(self):
        assert "python" in supported_test_runner_languages()

    def test_unknown_language_is_advisory_noop(self, tmp_path):
        result = ensure_test_runner_config(
            tmp_path, language="rust", source_dirs=["src/"], test_dirs=["tests/"]
        )
        assert result.action == "unsupported"
        # no files written: the verify honesty gate remains the authority.
        assert list(tmp_path.iterdir()) == []

    def test_missing_language_is_advisory_noop(self, tmp_path):
        result = ensure_test_runner_config(
            tmp_path, language=None, source_dirs=["src/"], test_dirs=["tests/"]
        )
        assert result.action == "unsupported"
        assert list(tmp_path.iterdir()) == []

    def test_defaults_when_scan_dirs_absent(self, tmp_path):
        # When no scan dirs are passed, conventional roots are used (never crash).
        result = ensure_test_runner_config(tmp_path, language="python", project_name="demo")
        assert result.action == "created"
        text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert 'testpaths = ["tests"]' in text
        # A-core: source root only, never ".".
        assert 'pythonpath = ["src"]' in text
        assert '"."' not in text


# ═══════════════════════════════════════════════════════════
# Anti-false-green: the ensured config does NOT mask failures
# ═══════════════════════════════════════════════════════════


class TestAntiFalseGreen:
    def test_ensured_runner_still_fails_when_a_test_fails(self, tmp_path):
        # The whole purpose is to make verify EXECUTE; it must still FAIL
        # honestly. A failing test must produce a non-zero pytest exit.
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "mymod.py").write_text("def add(a, b):\n    return a + b\n")
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_mymod.py").write_text(
            "from mymod import add\n\n\ndef test_add():\n    assert add(2, 3) == 999\n"
        )

        ensure_test_runner_config(
            tmp_path, language="python", source_dirs=["src/"], test_dirs=["tests/"]
        )
        command = detect_test_command(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m", *command.split()],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "1 failed" in proc.stdout

    def test_empty_suite_does_not_count_as_passed(self, tmp_path):
        # No tests at all: pytest's exit code 5 ("no tests collected") is
        # non-zero, so an empty suite is never a silent pass. The ensurer makes
        # the runner detectable; it does not invent green.
        (tmp_path / "tests").mkdir()
        ensure_test_runner_config(
            tmp_path, language="python", source_dirs=["src/"], test_dirs=["tests/"]
        )
        command = detect_test_command(tmp_path)
        proc = subprocess.run(
            [sys.executable, "-m", *command.split()],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0  # exit 5 = no tests collected
