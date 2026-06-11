"""Tests for codd.test_detection — the single test-command detector.

Covers the config-beats-detection precedence, every union heuristic rule,
and the equivalence of the backward-compat fixer wrapper.
"""

import json

import pytest

from codd.test_detection import detect_test_command


# ═══════════════════════════════════════════════════════════
# Precedence: explicit config > detection
# ═══════════════════════════════════════════════════════════


class TestConfigPrecedence:
    def test_fix_test_command_wins_over_everything(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        config = {
            "fix": {"test_command": "custom-fix-runner"},
            "verify": {"test_command": "custom-verify-runner"},
        }
        assert detect_test_command(tmp_path, config=config) == "custom-fix-runner"

    def test_verify_test_command_used_when_fix_absent(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        config = {"verify": {"test_command": "custom-verify-runner"}}
        assert detect_test_command(tmp_path, config=config) == "custom-verify-runner"

    def test_blank_config_value_falls_through_to_detection(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        config = {"fix": {"test_command": "  "}, "verify": {"test_command": None}}
        assert detect_test_command(tmp_path, config=config) == "pytest --tb=short -q"

    def test_no_config_means_detection(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/m\n")
        assert detect_test_command(tmp_path) == "go test ./..."


# ═══════════════════════════════════════════════════════════
# Union heuristics, in documented order
# ═══════════════════════════════════════════════════════════


class TestDetectionHeuristics:
    def test_pytest_ini_detected(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        assert detect_test_command(tmp_path) == "pytest --tb=short -q"

    def test_strong_pytest_config_beats_npm_scripts(self, tmp_path):
        # Rule 3 before rule 4: a configured pytest project with an
        # auxiliary package.json still runs pytest.
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname='x'\n[tool.pytest.ini_options]\naddopts='-q'\n"
        )
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        assert detect_test_command(tmp_path) == "pytest --tb=short -q"

    def test_setup_cfg_pytest_section_detected(self, tmp_path):
        (tmp_path / "setup.cfg").write_text("[tool:pytest]\naddopts = -q\n")
        assert detect_test_command(tmp_path) == "pytest --tb=short -q"

    def test_npm_script_test(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        assert detect_test_command(tmp_path) == "npm run test"

    def test_npm_prefers_unit_over_test_over_e2e(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest", "test:e2e": "playwright test", "test:unit": "vitest"}
        }))
        assert detect_test_command(tmp_path) == "npm run test:unit"

    def test_npm_e2e_used_as_last_script_resort(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"scripts": {"test:e2e": "playwright test"}})
        )
        assert detect_test_command(tmp_path) == "npm run test:e2e"

    def test_npm_scripts_beat_bare_pyproject(self, tmp_path):
        # Rule 4 before rule 5: explicit test script outranks the weak
        # "a pyproject.toml exists" signal.
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert detect_test_command(tmp_path) == "npm run test"

    def test_bare_pyproject_means_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert detect_test_command(tmp_path) == "pytest --tb=short -q"

    def test_vitest_dependency_without_script(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"vitest": "^1.0.0"}})
        )
        assert detect_test_command(tmp_path) == "npx vitest run"

    def test_jest_dependency_without_script(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"jest": "^29.0.0"}})
        )
        assert detect_test_command(tmp_path) == "npx jest"

    def test_cargo_project(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n")
        assert detect_test_command(tmp_path) == "cargo test"

    def test_go_project(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/m\n")
        assert detect_test_command(tmp_path) == "go test ./..."

    def test_bats_suite(self, tmp_path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "cli.bats").write_text("@test 'runs' { true; }\n")
        assert detect_test_command(tmp_path) == "bats -r ."

    def test_makefile_test_target_is_last_resort(self, tmp_path):
        (tmp_path / "Makefile").write_text("test:\n\t./run_tests.sh\n")
        assert detect_test_command(tmp_path) == "make test"

    def test_language_native_runner_beats_makefile(self, tmp_path):
        # Rule 8 before rule 10: go.mod outranks a generic make wrapper.
        (tmp_path / "go.mod").write_text("module example.com/m\n")
        (tmp_path / "Makefile").write_text("test:\n\tgo test ./...\n")
        assert detect_test_command(tmp_path) == "go test ./..."

    def test_makefile_without_test_target_ignored(self, tmp_path):
        (tmp_path / "Makefile").write_text("build:\n\tgcc main.c\n")
        assert detect_test_command(tmp_path) is None

    def test_invalid_package_json_is_tolerated(self, tmp_path):
        (tmp_path / "package.json").write_text("{not json")
        assert detect_test_command(tmp_path) is None

    def test_nothing_detected_returns_none(self, tmp_path):
        assert detect_test_command(tmp_path) is None


# ═══════════════════════════════════════════════════════════
# Backward-compat wrappers return identical results
# ═══════════════════════════════════════════════════════════


def _fixture_projects(tmp_path):
    """A spread of fixture projects exercising different heuristics."""
    projects = {}

    npm = tmp_path / "npm_project"
    npm.mkdir()
    (npm / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    projects["npm"] = npm

    py = tmp_path / "py_project"
    py.mkdir()
    (py / "pyproject.toml").write_text("[project]\nname='x'\n")
    projects["python"] = py

    mk = tmp_path / "make_project"
    mk.mkdir()
    (mk / "Makefile").write_text("test:\n\t./run.sh\n")
    projects["make"] = mk

    empty = tmp_path / "empty_project"
    empty.mkdir()
    projects["empty"] = empty

    return projects


class TestWrapperEquivalence:
    def test_fixer_wrapper_matches_unified_detector(self, tmp_path):
        from codd.fixer import _detect_test_command

        for name, root in _fixture_projects(tmp_path).items():
            assert _detect_test_command(root) == detect_test_command(root), name

    def test_fixer_run_local_tests_uses_config_precedence(self, tmp_path, monkeypatch):
        """fixer._run_local_tests must honor fix.test_command > verify >
        detection via the unified module."""
        import subprocess as _subprocess

        from codd import fixer

        captured: dict[str, str] = {}

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _Result()

        monkeypatch.setattr(fixer.subprocess, "run", fake_run)
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

        fixer._run_local_tests(tmp_path, {"verify": {"test_command": "verify-runner"}})
        assert captured["cmd"] == "verify-runner"

        fixer._run_local_tests(
            tmp_path,
            {"fix": {"test_command": "fix-runner"},
             "verify": {"test_command": "verify-runner"}},
        )
        assert captured["cmd"] == "fix-runner"

        fixer._run_local_tests(tmp_path, {})
        assert captured["cmd"] == "pytest --tb=short -q"
