"""Loadability tests for the mobile_ios_hig lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "mobile_ios_hig"
EXPECTED_KINDS = {
    "mobile_navigation_search_gap",
    "mobile_typography_gap",
    "mobile_color_gap",
    "mobile_accessibility_gap",
    "mobile_haptics_gap",
    "mobile_motion_gap",
    "mobile_input_gap",
    "mobile_layout_gap",
    "mobile_iconography_gap",
    "mobile_audio_gap",
    "mobile_privacy_gap",
    "mobile_feedback_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_mobile_ios_hig_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "mobile_ios_hig"
    assert "Apple HIG Mobile Observation Dimensions" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_mobile_ios_hig_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "mobile_ios_hig"
    assert manifest["lexicon_name"] == "mobile_ios_hig"
    assert manifest["source_url"] == (
        "https://developer.apple.com/design/human-interface-guidelines/"
    )
    assert "Apple Human Interface Guidelines" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 12
    assert manifest["references"]


def test_mobile_ios_hig_required_files_exist():
    for file_name in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / file_name).is_file()


def test_mobile_ios_hig_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
