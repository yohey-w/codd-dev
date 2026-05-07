"""Loadability tests for the web_responsive lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "web_responsive"
EXPECTED_KINDS = {
    "responsive_width_gap",
    "orientation_gap",
    "color_scheme_gap",
    "reduced_motion_gap",
    "resolution_gap",
    "hover_capability_gap",
    "pointer_accuracy_gap",
    "aspect_ratio_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_web_responsive_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "web_responsive"
    assert "Web Responsive Observation Dimensions" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_web_responsive_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "web_responsive"
    assert manifest["lexicon_name"] == "web_responsive"
    assert manifest["source_url"].startswith("https://developer.mozilla.org/")
    assert "MDN current" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 8
    assert manifest["references"]


def test_web_responsive_required_files_exist():
    for file_name in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / file_name).is_file()


def test_web_responsive_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
