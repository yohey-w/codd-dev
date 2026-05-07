"""Loadability tests for the web_authn_webauthn lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "web_authn_webauthn"
EXPECTED_KINDS = {
    "webauthn_registration_ceremony_gap",
    "webauthn_authentication_ceremony_gap",
    "webauthn_attestation_gap",
    "webauthn_user_verification_gap",
    "webauthn_credential_management_gap",
    "webauthn_extension_processing_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_web_authn_webauthn_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "web_authn_webauthn"
    assert "WebAuthn Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_web_authn_webauthn_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "web_authn_webauthn"
    assert manifest["lexicon_name"] == "web_authn_webauthn"
    assert manifest["source_url"] == "https://www.w3.org/TR/webauthn-3/"
    assert "Web Authentication Level 3" in manifest["source_version"]
    assert "Candidate Recommendation Snapshot 2026-01-13" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 6
    titles = {item["title"] for item in manifest["references"]}
    assert "Web Authentication: An API for accessing Public Key Credentials - Level 3" in titles
    assert "WebAuthn Extensions" in titles


def test_web_authn_webauthn_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_web_authn_webauthn_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )

