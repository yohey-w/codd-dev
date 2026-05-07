"""Loadability tests for the data_nosql_jsonschema lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "data_nosql_jsonschema"
EXPECTED_KINDS = {
    "jsonschema_document_gap",
    "jsonschema_vocabulary_gap",
    "jsonschema_meta_schema_gap",
    "jsonschema_identifier_gap",
    "jsonschema_reference_gap",
    "jsonschema_applicator_gap",
    "jsonschema_assertion_gap",
    "jsonschema_annotation_gap",
    "jsonschema_validation_keyword_gap",
    "jsonschema_format_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_data_nosql_jsonschema_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "data_nosql_jsonschema"
    assert "JSON Schema 2020-12 Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_data_nosql_jsonschema_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "data_nosql_jsonschema"
    assert manifest["lexicon_name"] == "data_nosql_jsonschema"
    assert manifest["source_url"] == "https://json-schema.org/specification"
    assert manifest["source_version"] == "JSON Schema 2020-12 Core + Validation"
    assert manifest["observation_dimensions"] == 10
    titles = {item["title"] for item in manifest["references"]}
    assert "JSON Schema Specification" in titles
    assert "JSON Schema Core 2020-12" in titles
    assert "JSON Schema Validation 2020-12" in titles


def test_data_nosql_jsonschema_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_data_nosql_jsonschema_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
