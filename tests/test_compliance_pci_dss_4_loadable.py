"""Loadability tests for the compliance_pci_dss_4 lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "compliance_pci_dss_4"
EXPECTED_KINDS = {
    "network_security_controls_gap",
    "stored_account_data_gap",
    "vulnerability_management_gap",
    "business_need_access_gap",
    "monitoring_testing_gap",
    "information_security_policy_gap",
    "transmission_cryptography_gap",
    "secure_software_gap",
    "personnel_screening_gap",
    "physical_access_gap",
    "incident_response_plan_gap",
    "pci_dss_scope_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_compliance_pci_dss_4_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "compliance_pci_dss_4"
    assert "PCI DSS v4.0 Compliance Observation Dimensions" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_compliance_pci_dss_4_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "compliance_pci_dss_4"
    assert manifest["lexicon_name"] == "compliance_pci_dss_4"
    assert manifest["source_url"].startswith(
        "https://www.pcisecuritystandards.org/document_library/"
    )
    assert "PCI DSS v4.0" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 12
    assert len(manifest["references"]) >= 2


def test_compliance_pci_dss_4_required_files_exist():
    for file_name in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / file_name).is_file()


def test_compliance_pci_dss_4_loaded_prompt_includes_base_prompt():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
