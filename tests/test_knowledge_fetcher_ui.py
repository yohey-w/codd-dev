import json

import yaml

import codd.planner as planner_module
from codd.knowledge_fetcher import KnowledgeFetcher
from codd.lexicon import LEXICON_FILENAME, validate_lexicon


def _write_package_json(tmp_path, dependencies):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": dependencies}),
        encoding="utf-8",
    )


def test_detect_react_from_package_json(tmp_path):
    _write_package_json(tmp_path, {"react": "^19.0.0"})

    stacks = KnowledgeFetcher(tmp_path).detect_tech_stack()

    assert "React" in stacks


def test_detect_vue_from_package_json(tmp_path):
    _write_package_json(tmp_path, {"vue": "^3.0.0"})

    stacks = KnowledgeFetcher(tmp_path).detect_tech_stack()

    assert "Vue" in stacks


def test_detect_svelte_from_package_json_scope(tmp_path):
    _write_package_json(tmp_path, {"@sveltejs/kit": "^2.0.0"})

    stacks = KnowledgeFetcher(tmp_path).detect_tech_stack()

    assert "Svelte" in stacks


def test_suggest_design_md_with_ui_stack_and_file(tmp_path):
    (tmp_path / "DESIGN.md").write_text("# Design\n", encoding="utf-8")

    suggestion = KnowledgeFetcher(tmp_path).suggest_design_md_for_ui(["React"])

    assert suggestion == {
        "ui_design_source": "DESIGN.md (found)",
        "spec": "https://github.com/google-labs-code/design.md",
    }


def test_suggest_design_md_missing_with_ui_stack(tmp_path):
    suggestion = KnowledgeFetcher(tmp_path).suggest_design_md_for_ui(["React"])

    assert suggestion is not None
    assert suggestion["ui_design_source"] == "DESIGN.md (recommended, not found)"
    assert "warning" in suggestion
    assert suggestion["spec"] == "https://github.com/google-labs-code/design.md"


def test_suggest_design_md_returns_none_without_ui_stack(tmp_path):
    assert KnowledgeFetcher(tmp_path).suggest_design_md_for_ui(["Python"]) is None


def test_ensure_lexicon_adds_design_md_suggestion_for_ui_project(tmp_path, monkeypatch):
    (tmp_path / "DESIGN.md").write_text("# Design\n", encoding="utf-8")
    monkeypatch.setattr(planner_module, "_detect_lexicon_context", lambda project_root: ["React"])

    planner_module._ensure_lexicon(tmp_path)

    data = yaml.safe_load((tmp_path / LEXICON_FILENAME).read_text(encoding="utf-8"))
    validate_lexicon(data)
    assert data["draft_context"]["ui_design"] == {
        "ui_design_source": "DESIGN.md (found)",
        "spec": "https://github.com/google-labs-code/design.md",
    }
    assert any(item["id"] == "design_token" for item in data["node_vocabulary"])
