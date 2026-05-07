"""Loadability tests for the mobile_a11y_native lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "mobile_a11y_native"
EXPECTED_KINDS = {
    "mobile_a11y_touchscreen_gap",
    "mobile_a11y_small_screen_gap",
    "mobile_a11y_input_modality_gap",
    "mobile_a11y_color_contrast_gap",
    "mobile_a11y_text_scaling_gap",
    "mobile_a11y_screen_reader_gap",
    "mobile_a11y_haptics_gap",
    "mobile_a11y_reduce_motion_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_mobile_a11y_native_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "mobile_a11y_native"
    assert "Native Mobile Accessibility Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_mobile_a11y_native_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "mobile_a11y_native"
    assert manifest["lexicon_name"] == "mobile_a11y_native"
    assert manifest["source_url"] == "https://www.w3.org/WAI/standards-guidelines/mobile/"
    assert manifest["source_version"].startswith("W3C Mobile Accessibility")
    assert manifest["observation_dimensions"] == 8
    titles = {item["title"] for item in manifest["references"]}
    assert "Mobile Accessibility at W3C" in titles
    assert "Apple Human Interface Guidelines: Accessibility" in titles
    assert "Android Developers: Make apps more accessible" in titles


def test_mobile_a11y_native_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_mobile_a11y_native_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
