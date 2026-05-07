"""Loadability tests for the data_governance_appi_gdpr lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "data_governance_appi_gdpr"
EXPECTED_KINDS = {
    "lawful_basis_gap",
    "consent_governance_gap",
    "purpose_limitation_gap",
    "retention_erasure_gap",
    "data_subject_rights_gap",
    "controller_accountability_gap",
    "processor_contract_gap",
    "processing_records_gap",
    "dpia_gap",
    "breach_notification_gap",
    "cross_border_transfer_gap",
    "dpo_contact_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_data_governance_appi_gdpr_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "data_governance_appi_gdpr"
    assert "GDPR and APPI Data Governance Observation Dimensions" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_data_governance_appi_gdpr_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "data_governance_appi_gdpr"
    assert manifest["lexicon_name"] == "data_governance_appi_gdpr"
    assert manifest["source_url"].startswith("https://gdpr-info.eu/")
    assert "GDPR" in manifest["source_version"]
    assert "APPI" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 12
    assert len(manifest["references"]) >= 3


def test_data_governance_appi_gdpr_required_files_exist():
    for file_name in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / file_name).is_file()


def test_data_governance_appi_gdpr_loaded_prompt_includes_base_prompt():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
