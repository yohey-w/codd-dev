"""Loadability tests for the data_eventsourcing_es_cqrs lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "data_eventsourcing_es_cqrs"
EXPECTED_KINDS = {
    "eventsourcing_pattern_gap",
    "eventsourcing_event_design_gap",
    "eventsourcing_event_store_gap",
    "eventsourcing_replay_gap",
    "eventsourcing_state_storage_gap",
    "eventsourcing_rebuild_gap",
    "eventsourcing_temporal_query_gap",
    "eventsourcing_cqrs_projection_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_data_eventsourcing_es_cqrs_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "data_eventsourcing_es_cqrs"
    assert "Event Sourcing and CQRS Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_data_eventsourcing_es_cqrs_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "data_eventsourcing_es_cqrs"
    assert manifest["lexicon_name"] == "data_eventsourcing_es_cqrs"
    assert manifest["source_url"] == "https://martinfowler.com/eaaDev/EventSourcing.html"
    assert manifest["source_version"].startswith("Fowler Event Sourcing")
    assert manifest["observation_dimensions"] == 8
    titles = {item["title"] for item in manifest["references"]}
    assert "Martin Fowler: Event Sourcing" in titles


def test_data_eventsourcing_es_cqrs_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_data_eventsourcing_es_cqrs_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
