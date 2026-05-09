"""Tests for the Domain-Driven Design lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "ddd_domain_driven_design"
EXPECTED_AXES = {
    "ubiquitous_language",
    "bounded_context",
    "aggregate_design",
    "entity_value_object",
    "domain_events",
    "repository_pattern",
    "application_service",
    "context_mapping",
    "anti_corruption_layer",
}
EXPECTED_KINDS = {
    "ddd_ubiquitous_language_gap",
    "ddd_bounded_context_gap",
    "ddd_aggregate_design_gap",
    "ddd_entity_value_object_gap",
    "ddd_domain_events_gap",
    "ddd_repository_pattern_gap",
    "ddd_application_service_gap",
    "ddd_context_mapping_gap",
    "ddd_anti_corruption_layer_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_ddd_domain_driven_design_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "ddd_domain_driven_design"
    assert "Domain-Driven Design Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_ddd_domain_driven_design_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["id"] == "ddd_domain_driven_design"
    assert manifest["name"] == "Domain-Driven Design (DDD)"
    assert manifest["lexicon_name"] == "ddd_domain_driven_design"
    assert manifest["version"] == "1.0.0"
    assert manifest["standard"] == "Evans 2003 / Vernon 2013"
    assert manifest["domain"] == "cross_industry"
    assert "Eric Evans" in manifest["source_version"]
    assert "Vaughn Vernon" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 9
    assert manifest["references"]


def test_ddd_domain_driven_design_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_ddd_domain_driven_design_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_ddd_domain_driven_design_axes_have_rationale_criticality_and_variants():
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    for axis in axes:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_ddd_domain_driven_design_severity_rules_cover_each_axis_gap():
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}

    for axis in EXPECTED_AXES:
        assert f"axis={axis} AND coverage=gap" in rule_conditions


def test_ddd_domain_driven_design_required_not_found_severity_rules():
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_by_condition = {rule["when"]: rule for rule in rules}

    assert (
        rule_by_condition["axis=bounded_context AND coverage=not_found"]["severity"]
        == "high"
    )
    assert (
        rule_by_condition["axis=ubiquitous_language AND coverage=not_found"][
            "severity"
        ]
        == "medium"
    )
    assert (
        rule_by_condition["axis=aggregate_design AND coverage=not_found"]["severity"]
        == "high"
    )


def test_ddd_domain_driven_design_extension_contains_coverage_examples():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap", "not_found"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content
