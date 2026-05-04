"""Tests for plan --init project_lexicon.yaml draft generation."""

from __future__ import annotations

import builtins
from pathlib import Path

import yaml

import codd.planner as planner_module
from codd.lexicon import LEXICON_FILENAME, validate_lexicon


QUESTIONS = """\
# Project Lexicon Questions

### Q01: URL path naming
**カテゴリ**: url_route

### Q02: DB table plurality
**カテゴリ**: db_model
"""


def test_ensure_lexicon_skips_existing_file(tmp_path, monkeypatch):
    lexicon_path = tmp_path / LEXICON_FILENAME
    original = """\
version: "1.0"
node_vocabulary: []
naming_conventions: []
design_principles:
  - Human-approved lexicon.
"""
    lexicon_path.write_text(original, encoding="utf-8")

    def fail_detect(project_root: Path) -> list[str]:
        raise AssertionError("existing lexicon should not invoke KnowledgeFetcher")

    monkeypatch.setattr(planner_module, "_detect_lexicon_context", fail_detect)

    planner_module._ensure_lexicon(tmp_path)

    assert lexicon_path.read_text(encoding="utf-8") == original


def test_ensure_lexicon_creates_draft_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(planner_module, "_detect_lexicon_context", lambda project_root: ["Python"])
    monkeypatch.setattr(planner_module, "_read_lexicon_questions", lambda: QUESTIONS)

    planner_module._ensure_lexicon(tmp_path)

    data = yaml.safe_load((tmp_path / LEXICON_FILENAME).read_text(encoding="utf-8"))
    assert data["project_id"] == tmp_path.name
    assert data["provenance"] == "inferred"
    assert data["confidence"] == 0.5
    assert data["draft_context"]["detected_context"] == ["Python"]
    assert data["draft_context"]["question_count"] == 2
    assert any("Detected project context: Python" in item for item in data["design_principles"])


def test_generated_draft_contains_required_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(planner_module, "_detect_lexicon_context", lambda project_root: ["Python"])
    monkeypatch.setattr(planner_module, "_read_lexicon_questions", lambda: QUESTIONS)

    planner_module._ensure_lexicon(tmp_path)

    data = yaml.safe_load((tmp_path / LEXICON_FILENAME).read_text(encoding="utf-8"))
    validate_lexicon(data)
    assert {"node_vocabulary", "naming_conventions", "design_principles"}.issubset(data)
    assert {"failure_modes", "extractor_registry"}.issubset(data)
    node_ids = {item["id"] for item in data["node_vocabulary"]}
    assert {"url_route", "db_table", "env_var", "cli_command"}.issubset(node_ids)
    assert all(item["provenance"] == "inferred" for item in data["node_vocabulary"])
    assert all(item["confidence"] == 0.5 for item in data["node_vocabulary"])


def test_ensure_lexicon_works_without_knowledge_fetcher(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "codd.knowledge_fetcher":
            raise ImportError("KnowledgeFetcher unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(planner_module, "_read_lexicon_questions", lambda: QUESTIONS)

    planner_module._ensure_lexicon(tmp_path)

    data = yaml.safe_load((tmp_path / LEXICON_FILENAME).read_text(encoding="utf-8"))
    validate_lexicon(data)
    assert data["draft_context"]["detected_context"] == []
    assert any("Detected project context: unknown" in item for item in data["design_principles"])
