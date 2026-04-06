"""Tests for codd require_plugins — entry-point backed plugin loading."""

from __future__ import annotations

from codd.require_plugins import (
    BUILTIN_PLUGIN,
    RequirePlugin,
    build_evidence_instructions,
    build_output_contract,
    build_tag_instructions,
    load_require_plugin,
)


class _FakeEntryPoint:
    def __init__(self, loaded):
        self._loaded = loaded

    def load(self):
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


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
    def test_fallback_to_builtin(self, monkeypatch):
        monkeypatch.setattr("codd.bridge.entry_points", lambda *, group=None: ())
        plugin = load_require_plugin()
        assert plugin.name == "builtin"
        assert len(plugin.inference_tags) == 5

    def test_registered_plugin(self, monkeypatch):
        plugin = RequirePlugin(
            name="test-pro",
            inference_tags=[{"name": "[custom]", "description": "custom"}],
            evidence_format="Evidence: src/file.py:symbol() + tests/test_file.py",
            output_sections=["- Custom section."],
            inference_guidelines=["- Custom guideline."],
        )

        def register(registry):
            registry.register_require_plugin(plugin)

        monkeypatch.setattr(
            "codd.bridge.entry_points",
            lambda *, group=None: (_FakeEntryPoint(register),),
        )

        loaded = load_require_plugin()
        assert loaded is plugin
        assert loaded.name == "test-pro"

    def test_invalid_plugin_falls_back(self, monkeypatch):
        monkeypatch.setattr(
            "codd.bridge.entry_points",
            lambda *, group=None: (_FakeEntryPoint(RuntimeError("broken")),),
        )

        plugin = load_require_plugin()
        assert plugin.name == "builtin"
