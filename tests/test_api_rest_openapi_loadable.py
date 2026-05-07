"""Loadability tests for the api_rest_openapi lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "api_rest_openapi"
EXPECTED_KINDS = {
    "openapi_document_gap",
    "api_version_gap",
    "problem_details_gap",
    "pagination_contract_gap",
    "authentication_scheme_gap",
    "schema_contract_gap",
    "content_negotiation_gap",
    "status_code_contract_gap",
    "hypermedia_link_gap",
    "idempotency_contract_gap",
    "rate_limit_contract_gap",
    "cors_policy_gap",
    "cache_header_gap",
    "parameter_validation_gap",
    "async_operation_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_api_rest_openapi_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "api_rest_openapi"
    assert "API REST OpenAPI Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_api_rest_openapi_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "api_rest_openapi"
    assert manifest["lexicon_name"] == "api_rest_openapi"
    assert manifest["source_url"] == "https://spec.openapis.org/oas/v3.1.0"
    assert "OpenAPI Specification 3.1.0" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 15
    titles = {item["title"] for item in manifest["references"]}
    assert "OpenAPI Specification v3.1.0" in titles
    assert "RFC 7807 - Problem Details for HTTP APIs" in titles
    assert "Fetch Standard - CORS protocol" in titles


def test_api_rest_openapi_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_api_rest_openapi_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
