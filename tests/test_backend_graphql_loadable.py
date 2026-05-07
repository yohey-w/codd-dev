"""Loadability tests for the backend_graphql lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "backend_graphql"
EXPECTED_KINDS = {
    "graphql_schema_gap",
    "graphql_type_system_gap",
    "graphql_type_extension_gap",
    "graphql_query_gap",
    "graphql_mutation_gap",
    "graphql_subscription_gap",
    "graphql_fragment_gap",
    "graphql_variable_gap",
    "graphql_directive_gap",
    "graphql_introspection_gap",
    "graphql_response_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_backend_graphql_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "backend_graphql"
    assert "GraphQL October 2021 Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_backend_graphql_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "backend_graphql"
    assert manifest["lexicon_name"] == "backend_graphql"
    assert manifest["source_url"] == "https://spec.graphql.org/October2021/"
    assert manifest["source_version"] == "GraphQL October 2021"
    assert manifest["observation_dimensions"] == 11
    titles = {item["title"] for item in manifest["references"]}
    assert "GraphQL Specification October 2021" in titles


def test_backend_graphql_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_backend_graphql_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
