"""Tests for codd.fixer module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codd.fixer import (
    FailureInfo,
    _build_fix_context,
    _build_fix_prompt,
    _detect_category_from_log,
    _detect_test_command,
    _extract_file_paths_from_log,
    _parse_ci_log,
    run_fix,
)


class TestExtractFilePathsFromLog:
    def test_extracts_typescript_paths(self):
        log = """
        FAIL tests/e2e/auth.spec.ts:42:10
        Error in src/auth/service.ts:15
        """
        paths = _extract_file_paths_from_log(log)
        assert "tests/e2e/auth.spec.ts" in paths
        assert "src/auth/service.ts" in paths

    def test_ignores_node_modules(self):
        log = "Error at node_modules/jest/index.js:10"
        paths = _extract_file_paths_from_log(log)
        assert len(paths) == 0

    def test_deduplicates(self):
        log = """
        src/auth.ts:10
        src/auth.ts:20
        """
        paths = _extract_file_paths_from_log(log)
        assert paths.count("src/auth.ts") == 1


class TestDetectCategoryFromLog:
    def test_detects_typecheck(self):
        assert _detect_category_from_log("error TS2345: Argument") == "typecheck"

    def test_detects_lint(self):
        assert _detect_category_from_log("ESLint found 3 errors") == "lint"

    def test_detects_build(self):
        assert _detect_category_from_log("Build failed with error") == "build"

    def test_detects_config_interactive_prompt(self):
        assert _detect_category_from_log("How would you like to configure ESLint?") == "config"

    def test_detects_config_setup_prompt(self):
        assert _detect_category_from_log("Would you like to set up TypeScript?") == "config"

    def test_defaults_to_test(self):
        assert _detect_category_from_log("some random output") == "test"


class TestParseCiLog:
    def test_parses_log_into_failure(self):
        log = "FAIL test/auth.spec.ts\nError: expected 200 got 500"
        failures = _parse_ci_log(log)
        assert len(failures) == 1
        assert failures[0].source == "ci"
        assert failures[0].category == "test"


class TestDetectTestCommand:
    def test_detects_npm_test(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({"scripts": {"test": "jest"}}))
        assert _detect_test_command(tmp_path) == "npm run test"

    def test_prefers_unit_over_e2e(self, tmp_path):
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({
            "scripts": {"test": "jest", "test:e2e": "playwright test", "test:unit": "vitest"}
        }))
        assert _detect_test_command(tmp_path) == "npm run test:unit"

    def test_detects_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'")
        assert _detect_test_command(tmp_path) == "pytest --tb=short -q"

    def test_returns_none_for_unknown(self, tmp_path):
        assert _detect_test_command(tmp_path) is None


class TestBuildFixPrompt:
    def test_includes_failure_info(self, tmp_path):
        failures = [FailureInfo(
            source="local",
            category="test",
            summary="Test auth failed",
            log="Error: expected 200 got 500",
            failed_files=["src/auth.ts"],
        )]
        config = {"project": {"name": "myapp", "language": "typescript"}}
        prompt = _build_fix_prompt(tmp_path, failures, "design context here", config)

        assert "Test auth failed" in prompt
        assert "expected 200 got 500" in prompt
        assert "src/auth.ts" in prompt
        assert "design context here" in prompt
        assert "myapp" in prompt

    def test_instructs_to_fix_implementation_not_tests(self, tmp_path):
        failures = [FailureInfo(
            source="local", category="test", summary="fail",
            log="error", failed_files=[],
        )]
        config = {"project": {"name": "x", "language": "python"}}
        prompt = _build_fix_prompt(tmp_path, failures, "", config)
        assert "IMPLEMENTATION" in prompt.upper()


class TestBuildFixContext:
    def test_finds_design_docs_with_matching_modules(self, tmp_path):
        # Create a design doc
        docs_dir = tmp_path / "docs" / "design"
        docs_dir.mkdir(parents=True)
        doc = docs_dir / "auth_design.md"
        doc.write_text(
            "---\ncodd:\n  node_id: auth-design\n  type: design\n  modules:\n    - auth\n---\n\n# Auth Design\n\nDetails here.\n"
        )

        config = {"scan": {"doc_dirs": ["docs/"]}}
        failures = [FailureInfo(
            source="local", category="test", summary="fail",
            log="error", failed_files=["src/auth/service.ts"],
        )]

        context = _build_fix_context(tmp_path, config, failures)
        assert "Auth Design" in context
        assert "auth_design.md" in context


class TestRunFix:
    def test_returns_fixed_when_no_failures(self, tmp_path):
        codd_dir = tmp_path / "codd"
        codd_dir.mkdir()
        (codd_dir / "codd.yaml").write_text("project:\n  name: test\n  language: python\n")

        with patch("codd.fixer._detect_ci_failures", return_value=[]), \
             patch("codd.fixer._run_local_tests", return_value=[]):
            result = run_fix(tmp_path)

        assert result.fixed is True
        assert len(result.attempts) == 0
