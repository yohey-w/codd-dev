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
    _collect_source_files,
    _detect_category_from_log,
    _detect_test_command,
    _extract_file_paths_from_log,
    _find_impl_candidates,
    _infer_impl_paths,
    _invoke_fix_ai,
    _is_test_path,
    _parse_ci_log,
    _prepare_fix_ai_command,
    _run_local_tests,
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


class TestInvokeFixAi:
    def test_applies_code_blocks_to_files(self, tmp_path):
        """AI returns fenced code blocks with file paths → written to disk."""
        ai_response = (
            "Here is the fix:\n\n"
            "```typescript src/api/route.ts\n"
            "export function GET() {\n"
            "  return Response.json({ ok: true });\n"
            "}\n"
            "```\n\n"
            "This adds the missing GET handler.\n"
        )
        with patch("codd.fixer._invoke_ai_command", return_value=ai_response):
            output = _invoke_fix_ai("claude --print", ai_response, tmp_path)

        target = tmp_path / "src" / "api" / "route.ts"
        assert target.exists()
        content = target.read_text()
        assert "export function GET()" in content
        assert "Response.json" in content

    def test_skips_files_outside_project(self, tmp_path):
        """Files with paths escaping project root are rejected."""
        ai_response = (
            "```python ../../etc/passwd\n"
            "malicious\n"
            "```\n"
        )
        with patch("codd.fixer._invoke_ai_command", return_value=ai_response):
            _invoke_fix_ai("claude --print", ai_response, tmp_path)

        assert not (tmp_path / "../../etc/passwd").exists()

    def test_handles_multiple_files(self, tmp_path):
        ai_response = (
            "```typescript src/a.ts\ncontent_a\n```\n\n"
            "```typescript src/b.ts\ncontent_b\n```\n"
        )
        with patch("codd.fixer._invoke_ai_command", return_value=ai_response):
            _invoke_fix_ai("claude --print", ai_response, tmp_path)

        assert (tmp_path / "src" / "a.ts").read_text() == "content_a\n"
        assert (tmp_path / "src" / "b.ts").read_text() == "content_b\n"

    def test_fallback_preceded_by_bold_path(self, tmp_path):
        """Fallback: **path/to/file** on line before code block."""
        ai_response = (
            "**src/handler.ts**:\n"
            "```typescript\n"
            "export function handler() { return 'fixed'; }\n"
            "```\n"
        )
        with patch("codd.fixer._invoke_ai_command", return_value=ai_response):
            _invoke_fix_ai("claude --print", ai_response, tmp_path)

        target = tmp_path / "src" / "handler.ts"
        assert target.exists()
        assert "fixed" in target.read_text()

    def test_fallback_comment_filepath(self, tmp_path):
        """Fallback: // filepath: path/to/file as first line in block."""
        ai_response = (
            "```typescript\n"
            "// filepath: src/utils.ts\n"
            "export const add = (a: number, b: number) => a + b;\n"
            "```\n"
        )
        with patch("codd.fixer._invoke_ai_command", return_value=ai_response):
            _invoke_fix_ai("claude --print", ai_response, tmp_path)

        target = tmp_path / "src" / "utils.ts"
        assert target.exists()
        assert "add" in target.read_text()

    def test_primary_pattern_takes_precedence(self, tmp_path):
        """Primary pattern wins when both primary and fallback match."""
        ai_response = (
            "```typescript src/route.ts\n"
            "primary_content\n"
            "```\n"
        )
        with patch("codd.fixer._invoke_ai_command", return_value=ai_response):
            _invoke_fix_ai("claude --print", ai_response, tmp_path)

        assert (tmp_path / "src" / "route.ts").read_text() == "primary_content\n"


class TestPrepareFixAiCommand:
    def test_replaces_document_system_prompt(self):
        cmd = (
            "claude --print --model opus --system-prompt "
            "'You are a technical document generator.'"
        )
        result = _prepare_fix_ai_command(cmd)
        assert "document generator" not in result
        assert "code repair" in result

    def test_adds_system_prompt_for_print_mode(self):
        cmd = "claude --print --model opus"
        result = _prepare_fix_ai_command(cmd)
        assert "--system-prompt" in result
        assert "code repair" in result

    def test_no_change_for_non_print_command(self):
        cmd = "codex exec --full-auto -m gpt-5.4"
        result = _prepare_fix_ai_command(cmd)
        assert "--system-prompt" not in result


class TestCollectSourceFiles:
    def test_reads_source_files_from_failures(self, tmp_path):
        src = tmp_path / "src" / "api.ts"
        src.parent.mkdir(parents=True)
        src.write_text("export function handler() {}")

        failures = [FailureInfo(
            source="ci", category="test", summary="fail",
            log="", failed_files=["src/api.ts"],
        )]
        result = _collect_source_files(tmp_path, failures)
        assert "export function handler()" in result
        assert "src/api.ts" in result

    def test_skips_test_files(self, tmp_path):
        test_file = tmp_path / "tests" / "test_foo.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def test_foo(): pass")

        failures = [FailureInfo(
            source="ci", category="test", summary="fail",
            log="", failed_files=["tests/test_foo.py"],
        )]
        result = _collect_source_files(tmp_path, failures)
        assert result == ""

    def test_strips_ci_runner_prefix(self, tmp_path):
        src = tmp_path / "src" / "handler.ts"
        src.parent.mkdir(parents=True)
        src.write_text("code here")

        failures = [FailureInfo(
            source="ci", category="test", summary="fail",
            log="",
            failed_files=["/home/runner/work/myrepo/myrepo/src/handler.ts"],
        )]
        result = _collect_source_files(tmp_path, failures)
        assert "code here" in result

    def test_infers_impl_from_test_paths(self, tmp_path):
        """When only test files are in failures, infer impl paths."""
        # Create a Next.js API route that should be found
        route = tmp_path / "src" / "app" / "api" / "enrollments" / "route.ts"
        route.parent.mkdir(parents=True)
        route.write_text("export function GET() { return Response.json([]); }")

        failures = [FailureInfo(
            source="ci", category="test", summary="fail",
            log="", failed_files=["tests/e2e/enrollments.spec.ts"],
        )]
        result = _collect_source_files(tmp_path, failures)
        assert "src/app/api/enrollments/route.ts" in result
        assert "export function GET()" in result


class TestIsTestPath:
    def test_tests_directory(self):
        assert _is_test_path("tests/e2e/auth.spec.ts") is True

    def test_spec_file(self):
        assert _is_test_path("src/auth.spec.ts") is True

    def test_test_file(self):
        assert _is_test_path("src/auth.test.ts") is True

    def test_python_test(self):
        assert _is_test_path("test_tasks.py") is True

    def test_impl_file(self):
        assert _is_test_path("src/app/api/auth/route.ts") is False

    def test_service_file(self):
        assert _is_test_path("src/services/auth.ts") is False


class TestInferImplPaths:
    def test_nextjs_api_route(self, tmp_path):
        route = tmp_path / "src" / "app" / "api" / "courses" / "route.ts"
        route.parent.mkdir(parents=True)
        route.write_text("handler")

        result = _infer_impl_paths(tmp_path, ["tests/e2e/courses.spec.ts"])
        assert "src/app/api/courses/route.ts" in result

    def test_kebab_case_variant(self, tmp_path):
        route = tmp_path / "src" / "app" / "api" / "lms-core" / "route.ts"
        route.parent.mkdir(parents=True)
        route.write_text("handler")

        result = _infer_impl_paths(tmp_path, ["tests/e2e/lms_core.spec.ts"])
        assert "src/app/api/lms-core/route.ts" in result

    def test_python_module(self, tmp_path):
        module = tmp_path / "tasks_api" / "app.py"
        module.parent.mkdir(parents=True)
        module.write_text("from flask import Flask")

        result = _infer_impl_paths(tmp_path, ["tests/test_tasks.py"])
        assert any("app.py" in p for p in result)

    def test_deduplicates(self, tmp_path):
        route = tmp_path / "src" / "app" / "api" / "auth" / "route.ts"
        route.parent.mkdir(parents=True)
        route.write_text("handler")

        result = _infer_impl_paths(tmp_path, [
            "tests/e2e/auth.spec.ts",
            "tests/unit/auth.test.ts",
        ])
        assert result.count("src/app/api/auth/route.ts") == 1


class TestRunLocalTestsNone:
    def test_returns_none_when_no_command(self, tmp_path):
        """No test command → None (unverified), not empty list (passed)."""
        result = _run_local_tests(tmp_path, {})
        assert result is None

    def test_returns_empty_on_success(self, tmp_path):
        config = {"fix": {"test_command": "true"}}
        result = _run_local_tests(tmp_path, config)
        assert result == []

    def test_returns_failures_on_error(self, tmp_path):
        config = {"fix": {"test_command": "false"}}
        result = _run_local_tests(tmp_path, config)
        assert result is not None
        assert len(result) > 0
