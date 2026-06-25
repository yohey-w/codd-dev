"""Regression tests for the Express (JS) brownfield dogfood findings.

Four independent bugs surfaced when running ``codd extract`` against a CommonJS
Express codebase:

* Bug 1 — the scan-path JS import parser (``_imports_ts_js``) only matched ESM
  ``import``/``from`` and was blind to CommonJS ``require()`` / dynamic
  ``import()``. It therefore reported NO internal dependencies for a CommonJS
  project, contradicting the DAG builder's ``_IMPORT_SPECIFIER_RE`` which DOES
  understand ``require(``. Two parsers disagreed.
* Bug 2 — the AI-extract prompt told the model to run ~15 bash enumeration
  blocks, but the default ``ai_command`` disables tools (``--tools ""``). Under
  no-tools the procedural bash framing pushed the model into agentic mode →
  stub/hang. The prompt must direct the model to extract from the embedded
  PROJECT CONTEXT when tools are unavailable.
* Bug 3 — the bootstrap codd.yaml template hardcoded ``test_dirs: ["tests/"]``
  and ``doc_dirs: ["docs/"]``. A project that uses ``test/`` (not ``tests/``)
  got all its tests excluded → vacuous task_completion.
* Bug 4 — route-path extraction read ``app.get('/user/:uid/...')`` strings out
  of JSDoc ``/** ... */`` comments (false positives).

Each test asserts the FIXED behaviour; where the report asked for
red-before-green the test would have failed against the pre-fix code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.parsing.regex_strategies import strategy_for


# ═══════════════════════════════════════════════════════════
# Bug 1: CommonJS require() / dynamic import() internal deps
# ═══════════════════════════════════════════════════════════


def _js_imports(tmp_path: Path, filename: str, content: str):
    """Run the scan-path JS import parser the way extract_facts does."""
    src_dir = tmp_path
    file_path = tmp_path / filename
    file_path.write_text(content, encoding="utf-8")
    return strategy_for("javascript").imports(content, tmp_path, src_dir, file_path)


class TestBug1CommonJsImports:
    def test_commonjs_require_is_internal_dependency(self, tmp_path):
        internal, external = _js_imports(
            tmp_path, "express.js", "const a = require('./a');\n"
        )
        # './a' resolves under src_dir -> internal "a" (was empty pre-fix).
        assert "a" in internal, internal

    def test_dynamic_import_is_internal_dependency(self, tmp_path):
        internal, _ = _js_imports(
            tmp_path, "lazy.js", "const m = await import('./helpers');\n"
        )
        assert "helpers" in internal, internal

    def test_external_require_goes_to_external(self, tmp_path):
        _, external = _js_imports(
            tmp_path, "app.js", "const express = require('express');\n"
        )
        assert "express" in external, external

    def test_scoped_require_keeps_scope(self, tmp_path):
        _, external = _js_imports(
            tmp_path, "app.js", "const x = require('@scope/pkg');\n"
        )
        assert "@scope/pkg" in external, external

    def test_esm_import_still_internal(self, tmp_path):
        # Generality guard: pre-existing ESM behaviour must not regress.
        internal, _ = _js_imports(
            tmp_path, "router.js", "import { a } from './a';\n"
        )
        assert "a" in internal, internal

    def test_esm_external_import_still_external(self, tmp_path):
        _, external = _js_imports(
            tmp_path, "router.js", "import express from 'express';\n"
        )
        assert "express" in external, external

    def test_require_and_esm_dont_double_count(self, tmp_path):
        # require('node:fs') -> external bare specifier (not internal).
        _, external = _js_imports(
            tmp_path, "io.js", "const fs = require('node:fs');\n"
        )
        assert "node:fs" in external, external


# ═══════════════════════════════════════════════════════════
# Bug 4: route paths inside JSDoc comments are NOT extracted
# ═══════════════════════════════════════════════════════════


class TestBug4RouteCommentFalsePositive:
    def _routes(self, content: str):
        # Exercise the DEFAULT JS extractor (tree-sitter), whose
        # _detect_typescript_code_patterns pulls route *path strings* out of
        # app.get(...) — that is the real false-positive site (not the regex
        # strategy's boolean-only _patterns_ts_js).
        from codd.extractor import ModuleInfo
        from codd.parsing import get_extractor

        mod = ModuleInfo(name="response")
        get_extractor("javascript").detect_code_patterns(mod, content)
        return mod.patterns.get("api_routes", "")

    def test_real_route_still_detected(self, tmp_path):
        routes = self._routes("app.get('/users', handler);\n")
        assert "/users" in routes or "HTTP route" in routes, routes

    def test_jsdoc_example_route_not_detected(self):
        # The exact express/lib/response.js:357 shape: a route string living in
        # a JSDoc @example block. It must NOT surface as an api_routes value.
        content = (
            "/**\n"
            " * @example\n"
            " *     app.get('/user/:uid/photos/:file', function(req, res){\n"
            " *       res.sendFile(...);\n"
            " *     });\n"
            " */\n"
            "exports.sendFile = function sendFile() {};\n"
        )
        routes = self._routes(content)
        assert "/user/:uid/photos/:file" not in routes, routes

    def test_line_comment_route_not_detected(self):
        content = "// app.get('/legacy', handler)\nmodule.exports = {};\n"
        routes = self._routes(content)
        assert "/legacy" not in routes, routes


# ═══════════════════════════════════════════════════════════
# Bug 3: bootstrap codd.yaml detects real test/doc dirs
# ═══════════════════════════════════════════════════════════


class TestBug3BootstrapDirDetection:
    def _bootstrap(self, tmp_path: Path) -> str:
        import yaml as _yaml

        from codd.cli import _ensure_bootstrap_codd_yaml

        config_path, generated = _ensure_bootstrap_codd_yaml(
            tmp_path,
            codd_dir=tmp_path / ".codd",
            language="javascript",
            source_dirs=["lib"],
        )
        assert generated
        text = config_path.read_text(encoding="utf-8")
        # The bootstrap also appends commented TODO stubs; the active config is
        # the first YAML document (stubs are comments, so safe_load ignores them).
        return _yaml.safe_load(text)

    def test_singular_test_dir_detected(self, tmp_path):
        # Express uses test/ (not tests/). Pre-fix the template hardcoded
        # tests/, excluding all 91 test files -> vacuous task_completion.
        (tmp_path / "lib").mkdir()
        (tmp_path / "test").mkdir()
        config = self._bootstrap(tmp_path)
        assert config["scan"]["test_dirs"] == ["test/"], config["scan"]

    def test_singular_doc_dir_detected(self, tmp_path):
        (tmp_path / "lib").mkdir()
        (tmp_path / "doc").mkdir()
        config = self._bootstrap(tmp_path)
        assert config["scan"]["doc_dirs"] == ["doc/"], config["scan"]

    def test_plural_dirs_still_detected(self, tmp_path):
        # Generality guard: the conventional tests/ + docs/ layout is unaffected.
        (tmp_path / "lib").mkdir()
        (tmp_path / "tests").mkdir()
        (tmp_path / "docs").mkdir()
        config = self._bootstrap(tmp_path)
        assert config["scan"]["test_dirs"] == ["tests/"], config["scan"]
        assert config["scan"]["doc_dirs"] == ["docs/"], config["scan"]

    def test_missing_dirs_fall_back_to_convention(self, tmp_path):
        # No test/ or doc/ present: keep a sensible default so the config stays
        # valid and editable (never empty list that silently scans nothing).
        (tmp_path / "lib").mkdir()
        config = self._bootstrap(tmp_path)
        assert config["scan"]["test_dirs"] == ["tests/"], config["scan"]
        assert config["scan"]["doc_dirs"] == ["docs/"], config["scan"]


# ═══════════════════════════════════════════════════════════
# Bug 2: AI-extract prompt works under no-tools (--tools "")
# ═══════════════════════════════════════════════════════════


class TestBug2NoToolsPrompt:
    def _prompt(self, *, ai_command: str) -> str:
        from codd.extract_ai import _build_prompt, ai_command_has_tools

        scan = _DummyScan()
        return _build_prompt(scan, tools_available=ai_command_has_tools(ai_command))

    def test_default_command_has_no_tools(self):
        from codd.extract_ai import ai_command_has_tools

        default_cmd = (
            'claude --print --permission-mode bypassPermissions '
            '--dangerously-skip-permissions --model claude-opus-4-8 --effort max --tools ""'
        )
        assert ai_command_has_tools(default_cmd) is False

    def test_command_with_tools_value_has_tools(self):
        from codd.extract_ai import ai_command_has_tools

        assert ai_command_has_tools('claude --print --tools "Bash Read"') is True

    def test_no_tools_prompt_forbids_shelling_out(self):
        prompt = self._prompt(ai_command='claude --print --tools ""')
        lowered = prompt.lower()
        # The no-tools prompt must direct context-only extraction and explicitly
        # tell the model NOT to call tools / run bash.
        assert "do not" in lowered
        assert ("do not run" in lowered) or ("do not call" in lowered) or (
            "no-tools" in lowered
        ) or ("without tools" in lowered)

    def test_no_tools_prompt_still_embeds_context(self):
        prompt = self._prompt(ai_command='claude --print --tools ""')
        # The embedded PROJECT CONTEXT must remain present — extraction reads it.
        assert "PROJECT CONTEXT" in prompt
        assert "lib/express.js" in prompt  # a source path from the dummy scan

    def test_tool_enabled_prompt_keeps_procedure(self):
        # Generality guard: when tools ARE available, the bash extraction
        # procedure must still be present (the tool-enabled path is preserved).
        prompt = self._prompt(ai_command='claude --print --tools "Bash Read"')
        assert "```bash" in prompt


class TestBug3TestFilesUnderTestDir:
    """A plain ``test/app.js`` (Mocha/Tape, no ``.test.`` infix) must register
    as a DAG test node — otherwise task_completion is vacuous even when
    test_dirs is correct (the deeper half of the Express dogfood finding)."""

    def test_plain_js_under_test_dir_is_test_file(self, tmp_path):
        from codd.dag.builder import _is_test_file

        (tmp_path / "test").mkdir()
        f = tmp_path / "test" / "app.js"
        f.write_text("describe('app', () => {});\n", encoding="utf-8")
        assert _is_test_file(f, tmp_path) is True

    def test_plain_js_under_tests_dir_is_test_file(self, tmp_path):
        from codd.dag.builder import _is_test_file

        (tmp_path / "tests").mkdir()
        f = tmp_path / "tests" / "router.js"
        f.write_text("it('routes', () => {});\n", encoding="utf-8")
        assert _is_test_file(f, tmp_path) is True

    def test_source_js_outside_test_dir_is_not_test_file(self, tmp_path):
        # Generality guard: a plain lib/*.js must NOT be misclassified as a test.
        from codd.dag.builder import _is_test_file

        (tmp_path / "lib").mkdir()
        f = tmp_path / "lib" / "express.js"
        f.write_text("module.exports = {};\n", encoding="utf-8")
        assert _is_test_file(f, tmp_path) is False

    def test_dot_test_infix_still_recognized(self, tmp_path):
        from codd.dag.builder import _is_test_file

        f = tmp_path / "src" / "foo.test.ts"
        f.parent.mkdir()
        f.write_text("", encoding="utf-8")
        assert _is_test_file(f, tmp_path) is True


class _DummyScan:
    """Minimal PreScanResult-shaped stand-in for prompt building (no I/O)."""

    directory_tree = "lib/\n  express.js\n"
    framework_files: dict = {}
    config_files: dict = {"package.json": '{"name": "express"}'}
    source_files = {"lib/express.js": "const app = require('./application');\n"}
    iac_files: dict = {}
    test_files = ["test/app.js"]
    test_file_contents: dict = {}
