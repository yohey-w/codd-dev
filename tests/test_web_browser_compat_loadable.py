"""Loadability tests for the web_browser_compat lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "web_browser_compat"
EXPECTED_KINDS = {
    "browser_compat_newly_available_gap",
    "browser_compat_widely_available_gap",
    "browser_compat_limited_availability_gap",
    "browser_compat_core_browser_set_gap",
    "browser_compat_baseline_threshold_gap",
    "browser_compat_polyfill_gap",
    "browser_compat_progressive_enhancement_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_web_browser_compat_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "web_browser_compat"
    assert "Browser Compatibility Baseline Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_web_browser_compat_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "web_browser_compat"
    assert manifest["lexicon_name"] == "web_browser_compat"
    assert manifest["source_url"] == "https://web.dev/baseline"
    assert "Baseline 2024" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 7
    titles = {item["title"] for item in manifest["references"]}
    assert "Baseline" in titles
    assert "Baseline compatibility" in titles


def test_web_browser_compat_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_web_browser_compat_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
