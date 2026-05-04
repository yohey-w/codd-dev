"""Tests for codd validate --design-tokens."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

from click.testing import CliRunner

from codd.cli import main
from codd.validator import validate_design_tokens


@dataclass(frozen=True)
class _Token:
    id: str
    value: str


@dataclass(frozen=True)
class _ExtractResult:
    tokens: list[_Token]
    error: str | None = None


def _install_design_md(monkeypatch, tokens: list[_Token], error: str | None = None) -> None:
    module = types.ModuleType("codd.design_md")

    class DesignMdExtractor:
        def extract(self, path):
            return _ExtractResult(tokens=tokens, error=error)

    module.DesignMdExtractor = DesignMdExtractor
    monkeypatch.setitem(sys.modules, "codd.design_md", module)


def _write_design_md(project_root) -> None:
    (project_root / "DESIGN.md").write_text("# Design tokens\n", encoding="utf-8")


def test_no_violations_when_using_tokens(tmp_path, monkeypatch):
    _write_design_md(tmp_path)
    _install_design_md(monkeypatch, [_Token("colors.Primary", "#1A73E8")])
    (tmp_path / "App.tsx").write_text("const color = '{colors.Primary}';\n", encoding="utf-8")

    assert validate_design_tokens(tmp_path) == []


def test_hex_violation_detected_with_token_suggestion(tmp_path, monkeypatch):
    _write_design_md(tmp_path)
    _install_design_md(monkeypatch, [_Token("colors.Primary", "#1A73E8")])
    (tmp_path / "App.tsx").write_text("const style = { color: '#1A73E8' };\n", encoding="utf-8")

    violations = validate_design_tokens(tmp_path)

    assert len(violations) == 1
    assert violations[0].file == "App.tsx"
    assert violations[0].line == 1
    assert violations[0].pattern == "#1a73e8"
    assert violations[0].suggestion == "colors.Primary"


def test_px_violation_detected_when_value_is_not_a_token(tmp_path, monkeypatch):
    _write_design_md(tmp_path)
    _install_design_md(monkeypatch, [_Token("spacing.Small", "8px")])
    (tmp_path / "App.svelte").write_text("<div style='margin: 13px'></div>\n", encoding="utf-8")

    violations = validate_design_tokens(tmp_path)

    assert len(violations) == 1
    assert violations[0].file == "App.svelte"
    assert violations[0].pattern == "13px"
    assert violations[0].suggestion == "no matching token"


def test_no_design_md_returns_empty(tmp_path):
    (tmp_path / "App.tsx").write_text("const style = { color: '#1A73E8' };\n", encoding="utf-8")

    assert validate_design_tokens(tmp_path) == []


def test_non_ui_file_ignored(tmp_path, monkeypatch):
    _write_design_md(tmp_path)
    _install_design_md(monkeypatch, [_Token("colors.Primary", "#1A73E8")])
    (tmp_path / "app.py").write_text("COLOR = '#1A73E8'\n", encoding="utf-8")

    assert validate_design_tokens(tmp_path) == []


def test_design_tokens_cli_reports_violations(tmp_path, monkeypatch):
    _write_design_md(tmp_path)
    _install_design_md(monkeypatch, [_Token("colors.Primary", "#1A73E8")])
    (tmp_path / "App.tsx").write_text("const style = { color: '#1A73E8' };\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["validate", "--design-tokens", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "Design token violations found: 1" in result.output
    assert "App.tsx:1 - hardcoded #1a73e8 (suggest: colors.Primary)" in result.output
