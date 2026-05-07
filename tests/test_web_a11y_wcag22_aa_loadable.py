"""Loadability tests for the web_a11y_wcag22_aa lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "web_a11y_wcag22_aa"
EXPECTED_KINDS = {
    "text_alternative_gap",
    "media_alternative_gap",
    "semantic_structure_gap",
    "contrast_reflow_gap",
    "keyboard_access_gap",
    "timing_control_gap",
    "seizure_safety_gap",
    "navigation_focus_gap",
    "input_modality_gap",
    "language_gap",
    "predictable_behavior_gap",
    "input_assistance_gap",
    "assistive_technology_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_web_a11y_wcag22_aa_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "web_a11y_wcag22_aa"
    assert "WCAG 2.2 AA Observation Dimensions" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_web_a11y_wcag22_aa_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "web_a11y_wcag22_aa"
    assert manifest["lexicon_name"] == "web_a11y_wcag22_aa"
    assert manifest["source_url"] == "https://www.w3.org/TR/WCAG22/"
    assert "W3C Recommendation" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 13
    assert manifest["references"]


def test_web_a11y_wcag22_aa_required_files_exist():
    for file_name in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / file_name).is_file()


def test_web_a11y_wcag22_aa_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
