"""Loadability tests for the web_performance_core_web_vitals lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "web_performance_core_web_vitals"
EXPECTED_KINDS = {
    "web_vitals_lcp_gap",
    "web_vitals_inp_gap",
    "web_vitals_cls_gap",
    "web_vitals_ttfb_gap",
    "web_vitals_fcp_gap",
    "web_vitals_tti_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_web_performance_core_web_vitals_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "web_performance_core_web_vitals"
    assert "Core Web Vitals Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_web_performance_core_web_vitals_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "web_performance_core_web_vitals"
    assert manifest["lexicon_name"] == "web_performance_core_web_vitals"
    assert manifest["source_url"] == "https://web.dev/articles/vitals"
    assert "Core Web Vitals" in manifest["source_version"]
    assert "W3C Web Performance" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 6
    titles = {item["title"] for item in manifest["references"]}
    assert "web.dev - Web Vitals" in titles
    assert "W3C Web Performance Working Group" in titles


def test_web_performance_core_web_vitals_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_web_performance_core_web_vitals_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )

