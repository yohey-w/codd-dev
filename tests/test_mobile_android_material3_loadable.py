"""Loadability tests for the mobile_android_material3 lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "mobile_android_material3"
EXPECTED_KINDS = {
    "material3_color_gap",
    "material3_typography_gap",
    "material3_shape_gap",
    "material3_elevation_gap",
    "material3_motion_gap",
    "material3_component_gap",
    "material3_iconography_gap",
    "material3_accessibility_gap",
    "material3_adaptive_design_gap",
    "material3_interaction_state_gap",
    "material3_content_design_gap",
    "material3_dynamic_color_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_mobile_android_material3_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "mobile_android_material3"
    assert (
        "Material Design 3 Mobile Observation Dimensions"
        in config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_mobile_android_material3_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "mobile_android_material3"
    assert manifest["lexicon_name"] == "mobile_android_material3"
    assert manifest["source_url"] == "https://m3.material.io/foundations"
    assert "Material Design 3" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 12
    assert manifest["references"]


def test_mobile_android_material3_required_files_exist():
    for file_name in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / file_name).is_file()


def test_mobile_android_material3_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
