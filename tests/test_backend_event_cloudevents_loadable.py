"""Loadability tests for the backend_event_cloudevents lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "backend_event_cloudevents"
EXPECTED_KINDS = {
    "cloudevents_context_attributes_gap",
    "cloudevents_required_attributes_gap",
    "cloudevents_optional_attributes_gap",
    "cloudevents_type_system_gap",
    "cloudevents_extension_attributes_gap",
    "cloudevents_event_data_gap",
    "cloudevents_protocol_binding_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_backend_event_cloudevents_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "backend_event_cloudevents"
    assert "CloudEvents 1.0.2 Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_backend_event_cloudevents_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "backend_event_cloudevents"
    assert manifest["lexicon_name"] == "backend_event_cloudevents"
    assert manifest["source_url"] == (
        "https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md"
    )
    assert manifest["source_version"] == "CloudEvents 1.0.2"
    assert manifest["observation_dimensions"] == 7
    titles = {item["title"] for item in manifest["references"]}
    assert "CloudEvents Specification 1.0.2" in titles
    assert "CloudEvents JSON Event Format" in titles
    assert "CloudEvents HTTP Protocol Binding" in titles


def test_backend_event_cloudevents_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_backend_event_cloudevents_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
