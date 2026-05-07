"""Loadability tests for the compliance_hipaa lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "compliance_hipaa"
EXPECTED_KINDS = {
    "administrative_safeguards_gap",
    "physical_safeguards_gap",
    "technical_safeguards_gap",
    "risk_analysis_gap",
    "access_control_gap",
    "audit_controls_gap",
    "integrity_controls_gap",
    "transmission_security_gap",
    "breach_notification_gap",
    "business_associate_contract_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_compliance_hipaa_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "compliance_hipaa"
    assert "HIPAA Compliance Observation Dimensions" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_compliance_hipaa_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "compliance_hipaa"
    assert manifest["lexicon_name"] == "compliance_hipaa"
    assert manifest["source_url"].startswith("https://www.hhs.gov/hipaa/")
    assert "HIPAA Security Rule" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 10
    assert len(manifest["references"]) >= 3


def test_compliance_hipaa_required_files_exist():
    for file_name in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / file_name).is_file()


def test_compliance_hipaa_loaded_prompt_includes_base_prompt():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
