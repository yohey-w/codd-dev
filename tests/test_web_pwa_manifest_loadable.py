"""Loadability tests for the web_pwa_manifest lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "web_pwa_manifest"
EXPECTED_KINDS = {
    "pwa_manifest_members_gap",
    "pwa_manifest_icons_gap",
    "pwa_manifest_display_gap",
    "pwa_manifest_start_url_gap",
    "pwa_manifest_scope_gap",
    "pwa_manifest_theme_color_gap",
    "pwa_manifest_shortcuts_gap",
    "pwa_manifest_share_target_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_web_pwa_manifest_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "web_pwa_manifest"
    assert "Web App Manifest Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_web_pwa_manifest_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "web_pwa_manifest"
    assert manifest["lexicon_name"] == "web_pwa_manifest"
    assert manifest["source_url"] == "https://www.w3.org/TR/appmanifest/"
    assert "Web Application Manifest" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 8
    titles = {item["title"] for item in manifest["references"]}
    assert "Web Application Manifest" in titles
    assert "Web Share Target API" in titles


def test_web_pwa_manifest_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_web_pwa_manifest_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
