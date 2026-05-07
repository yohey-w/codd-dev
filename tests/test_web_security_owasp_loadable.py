"""Tests for the OWASP web security elicitation lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "web_security_owasp"


def _read_yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_web_security_owasp_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "web_security_owasp"
    assert "OWASP Web Security Coverage Lexicon" in config.prompt_extension_content
    assert len(config.recommended_kinds) == 15


def test_web_security_owasp_manifest_declares_official_sources():
    manifest = _read_yaml("manifest.yaml")

    assert manifest["source_url"] == "https://owasp.org/Top10/2021/"
    assert manifest["source_version"] == "OWASP Top 10:2021 + OWASP ASVS 4.0.3"
    assert manifest["observation_dimensions"] == 14
    titles = {item["title"] for item in manifest["references"]}
    assert "OWASP Top 10:2021" in titles
    assert "OWASP Application Security Verification Standard" in titles


def test_web_security_owasp_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_web_security_owasp_recommended_kinds_are_unique_strings():
    payload = _read_yaml("recommended_kinds.yaml")
    kinds = payload["recommended_kinds"]

    assert len(kinds) == len(set(kinds))
    assert all(isinstance(kind, str) and kind for kind in kinds)
