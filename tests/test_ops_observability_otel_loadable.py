"""Loadability tests for the ops_observability_otel lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "ops_observability_otel"
EXPECTED_KINDS = {
    "trace_coverage_gap",
    "metric_coverage_gap",
    "log_coverage_gap",
    "propagation_context_gap",
    "resource_identity_gap",
    "instrumentation_api_gap",
    "collector_pipeline_gap",
    "semantic_convention_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_ops_observability_otel_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "ops_observability_otel"
    assert "OpenTelemetry Observability Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_ops_observability_otel_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "ops_observability_otel"
    assert manifest["lexicon_name"] == "ops_observability_otel"
    assert manifest["source_url"] == "https://opentelemetry.io/docs/specs/otel/"
    assert "OpenTelemetry Specification 1.34" in manifest["source_version"]
    assert "Semantic Conventions" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 8
    titles = {item["title"] for item in manifest["references"]}
    assert "OpenTelemetry Specification" in titles
    assert "OpenTelemetry Semantic Conventions" in titles
    assert "OpenTelemetry Collector Architecture" in titles


def test_ops_observability_otel_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_ops_observability_otel_loaded_prompt_includes_base_prompt():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
