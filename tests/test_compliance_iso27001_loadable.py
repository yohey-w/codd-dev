"""Loadability tests for the compliance_iso27001 lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "compliance_iso27001"


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_compliance_iso27001_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "compliance_iso27001"
    assert "ISO/IEC 27001 Compliance Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )


def test_compliance_iso27001_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "compliance_iso27001"
    assert manifest["lexicon_name"] == "compliance_iso27001"
    assert manifest["source_url"] == "https://www.iso.org/standard/27001"
    assert manifest["source_version"] == "ISO/IEC 27001:2022"
    assert manifest["observation_dimensions"] == 14


def test_compliance_iso27001_recommended_kinds_load():
    config = load_lexicon(LEXICON_ROOT)

    assert "statement_of_applicability_gap" in set(config.recommended_kinds)
    assert "incident_management_gap" in set(config.recommended_kinds)

