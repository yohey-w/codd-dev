"""Tests for codd require_plugins — plugin system for pro/enterprise features."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.require_plugins import (
    BUILTIN_PLUGIN,
    RequirePlugin,
    build_evidence_instructions,
    build_output_contract,
    build_tag_instructions,
    load_require_plugin,
)


class TestBuiltinPlugin:
    def test_has_five_tags(self):
        assert len(BUILTIN_PLUGIN.inference_tags) == 5
        names = [t["name"] for t in BUILTIN_PLUGIN.inference_tags]
        assert "[observed]" in names
        assert "[inferred]" in names
        assert "[speculative]" in names
        assert "[unknown]" in names
        assert "[contradictory]" in names

    def test_has_evidence_format(self):
        assert BUILTIN_PLUGIN.evidence_format is not None
        assert "Evidence:" in BUILTIN_PLUGIN.evidence_format

    def test_has_output_sections(self):
        assert len(BUILTIN_PLUGIN.output_sections) == 1
        assert "Human Review Issues" in BUILTIN_PLUGIN.output_sections[0]

    def test_has_guidelines(self):
        assert len(BUILTIN_PLUGIN.inference_guidelines) > 0


class TestBuildTagInstructions:
    def test_builtin_tags(self):
        lines = build_tag_instructions(BUILTIN_PLUGIN)
        text = "\n".join(lines)
        assert "[observed]" in text
        assert "[inferred]" in text
        assert "[speculative]" in text
        assert "[unknown]" in text
        assert "[contradictory]" in text

    def test_custom_tags(self):
        plugin = RequirePlugin(
            inference_tags=[
                {"name": "[custom]", "description": "a custom tag"},
            ],
        )
        lines = build_tag_instructions(plugin)
        assert any("[custom]" in line for line in lines)


class TestBuildEvidenceInstructions:
    def test_builtin_has_evidence_format(self):
        lines = build_evidence_instructions(BUILTIN_PLUGIN)
        assert len(lines) == 2
        assert "Evidence:" in lines[1]

    def test_no_format_returns_empty(self):
        plugin = RequirePlugin(evidence_format=None)
        lines = build_evidence_instructions(plugin)
        assert lines == []

    def test_with_custom_format(self):
        plugin = RequirePlugin(
            evidence_format="Evidence: src/foo.py:bar() + tests/test_foo.py",
        )
        lines = build_evidence_instructions(plugin)
        assert len(lines) == 2
        assert "src/foo.py" in lines[1]


class TestBuildOutputContract:
    def test_builtin_has_human_review(self):
        lines = build_output_contract(BUILTIN_PLUGIN)
        assert len(lines) == 1
        assert "Human Review Issues" in lines[0]

    def test_with_custom_sections(self):
        plugin = RequirePlugin(
            output_sections=["- Include Approval Ledger section."],
        )
        lines = build_output_contract(plugin)
        assert len(lines) == 1
        assert "Approval Ledger" in lines[0]


class TestLoadRequirePlugin:
    def test_fallback_to_builtin(self, tmp_path):
        plugin = load_require_plugin(tmp_path)
        assert plugin.name == "builtin"
        assert len(plugin.inference_tags) == 5

    def test_project_local_plugin(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        codd_dir = project / "codd"
        codd_dir.mkdir()
        config = {"scan": {"source_dirs": [], "test_dirs": [], "doc_dirs": [], "config_files": [], "exclude": []}}
        (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

        plugins_dir = codd_dir / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "require.py").write_text(
            '''
PLUGIN_NAME = "test-pro"
INFERENCE_TAGS = [
    {"name": "[observed]", "description": "directly evidenced"},
    {"name": "[inferred]", "description": "reasonable inference"},
    {"name": "[speculative]", "description": "weak evidence"},
    {"name": "[unknown]", "description": "no evidence"},
    {"name": "[contradictory]", "description": "conflicting evidence"},
]
EVIDENCE_FORMAT = "Evidence: src/file.py:symbol() + tests/test_file.py"
OUTPUT_SECTIONS = [
    "- Human Review Issues: prioritized list for human judgment.",
]
INFERENCE_GUIDELINES = [
    "- Do not invent features.",
]
''',
            encoding="utf-8",
        )

        plugin = load_require_plugin(project)
        assert plugin.name == "test-pro"
        assert len(plugin.inference_tags) == 5
        assert plugin.evidence_format is not None
        assert len(plugin.output_sections) == 1

    def test_invalid_plugin_falls_back(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        codd_dir = project / "codd"
        codd_dir.mkdir()
        config = {"scan": {"source_dirs": [], "test_dirs": [], "doc_dirs": [], "config_files": [], "exclude": []}}
        (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

        plugins_dir = codd_dir / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "require.py").write_text("raise SyntaxError('broken')", encoding="utf-8")

        plugin = load_require_plugin(project)
        assert plugin.name == "builtin"  # Falls back gracefully
