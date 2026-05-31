"""Coverage and loadability tests for the data_aggregation_policies lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "data_aggregation_policies"
EXPECTED_AXES = {
    "collection_cardinality",
    "test_data_variation",
    "aggregation_function",
    "empty_partial_state",
    "grouping_dimension",
    "recency_selection",
    "source_traceability",
}
EXPECTED_KINDS = {
    "cardinality_display_gap",
    "test_data_variation_gap",
    "aggregation_policy_gap",
    "empty_partial_state_gap",
    "grouping_dimension_gap",
    "recency_selection_gap",
    "source_traceability_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_data_aggregation_policies_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "data_aggregation_policies"
    assert "Data Aggregation Policy Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_data_aggregation_policies_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_data_aggregation_policies_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_data_aggregation_policies_axes_have_test_data_variation_obligation():
    axes = {axis["axis_type"]: axis for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    variant_ids = {variant["id"] for variant in axes["test_data_variation"]["variants"]}

    assert {"seed_zero_records", "seed_one_record", "seed_many_records"} <= variant_ids


def test_data_aggregation_policies_severity_rules_cover_each_axis_gap():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions

    severity_by_condition = {rule["when"]: rule["severity"] for rule in rules}
    assert severity_by_condition["axis=collection_cardinality AND coverage=gap"] == "high"
    assert severity_by_condition["axis=test_data_variation AND coverage=gap"] == "high"
    assert severity_by_condition["axis=empty_partial_state AND coverage=gap"] == "medium"
