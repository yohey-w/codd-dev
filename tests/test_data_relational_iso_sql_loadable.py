"""Loadability tests for the data_relational_iso_sql lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "data_relational_iso_sql"
EXPECTED_KINDS = {
    "sql_schema_definition_gap",
    "sql_table_definition_gap",
    "sql_data_type_gap",
    "sql_domain_constraint_gap",
    "sql_referential_constraint_gap",
    "sql_query_expression_gap",
    "sql_data_change_statement_gap",
    "sql_transaction_statement_gap",
    "sql_isolation_level_gap",
    "sql_view_contract_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_data_relational_iso_sql_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "data_relational_iso_sql"
    assert "Relational ISO SQL Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_data_relational_iso_sql_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "data_relational_iso_sql"
    assert manifest["lexicon_name"] == "data_relational_iso_sql"
    assert manifest["source_url"] == "https://www.iso.org/standard/76583.html"
    assert "ISO/IEC 9075-1:2023" in manifest["source_version"]
    assert "ISO/IEC 9075-2:2023" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 10
    titles = {item["title"] for item in manifest["references"]}
    assert "ISO/IEC 9075-1:2023 - SQL/Framework" in titles
    assert "ISO/IEC 9075-2:2023 - SQL/Foundation" in titles


def test_data_relational_iso_sql_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_data_relational_iso_sql_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
