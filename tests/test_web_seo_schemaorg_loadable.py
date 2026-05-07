"""Loadability tests for the web_seo_schemaorg lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "web_seo_schemaorg"
EXPECTED_KINDS = {
    "schemaorg_organization_gap",
    "schemaorg_person_gap",
    "schemaorg_product_gap",
    "schemaorg_article_gap",
    "schemaorg_breadcrumb_list_gap",
    "schemaorg_faq_page_gap",
    "schemaorg_event_gap",
    "schemaorg_video_object_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_web_seo_schemaorg_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "web_seo_schemaorg"
    assert "Schema.org SEO Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_web_seo_schemaorg_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "web_seo_schemaorg"
    assert manifest["lexicon_name"] == "web_seo_schemaorg"
    assert manifest["source_url"] == "https://schema.org/docs/full.html"
    assert "Schema.org" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 8
    titles = {item["title"] for item in manifest["references"]}
    assert "Schema.org full hierarchy" in titles


def test_web_seo_schemaorg_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_web_seo_schemaorg_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
