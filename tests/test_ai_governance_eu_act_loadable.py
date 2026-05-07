"""Loadability tests for the ai_governance_eu_act lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "ai_governance_eu_act"
EXPECTED_KINDS = {
    "ai_risk_classification_gap",
    "prohibited_ai_practice_gap",
    "high_risk_requirement_gap",
    "human_oversight_gap",
    "ai_transparency_gap",
    "gpai_obligation_gap",
    "conformity_assessment_gap",
    "post_market_monitoring_gap",
    "fundamental_rights_impact_gap",
    "ai_governance_authority_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_ai_governance_eu_act_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "ai_governance_eu_act"
    assert "EU AI Act Governance Observation Dimensions" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_ai_governance_eu_act_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "ai_governance_eu_act"
    assert manifest["lexicon_name"] == "ai_governance_eu_act"
    assert manifest["source_url"].startswith("https://eur-lex.europa.eu/")
    assert "2024/1689" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 10
    assert manifest["references"]


def test_ai_governance_eu_act_required_files_exist():
    for file_name in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / file_name).is_file()


def test_ai_governance_eu_act_loaded_prompt_includes_base_prompt():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
