"""Loadability and coverage-axis tests for the ml_model_cards lexicon."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "ml_model_cards"
EXPECTED_AXES = {
    "model_details",
    "intended_use",
    "evaluation_factors",
    "performance_metrics",
    "training_data",
    "ethical_considerations",
    "caveats_recommendations",
    "model_versioning",
}
EXPECTED_KINDS = {f"{axis}_gap" for axis in EXPECTED_AXES}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_ml_model_cards_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "ml_model_cards"
    assert "ML Model Cards Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )


def test_ml_model_cards_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "ml_model_cards"
    assert manifest["lexicon_name"] == "ml_model_cards"
    assert manifest["standard"] == "Mitchell et al. 2019 / Google Model Card Toolkit"
    assert manifest["domain"] == "cross_industry"
    assert manifest["source_url"] == "https://arxiv.org/abs/1810.03993"
    assert manifest["observation_dimensions"] == 8
    titles = {item["title"] for item in manifest["references"]}
    assert "Model Cards for Model Reporting" in titles


def test_ml_model_cards_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_ml_model_cards_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_ml_model_cards_axes_have_rationale_criticality_and_variants():
    for axis in _yaml("lexicon.yaml")["coverage_axes"]:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_ml_model_cards_variant_ids_are_model_card_literals():
    variant_ids = {
        variant["id"]
        for axis in _yaml("lexicon.yaml")["coverage_axes"]
        for variant in axis["variants"]
    }

    for expected in (
        "Model Details",
        "Intended Use",
        "Factors",
        "Metrics",
        "Training Data",
        "Ethical Considerations",
        "Caveats and Recommendations",
        "model registry",
        "rollback strategy",
    ):
        assert expected in variant_ids


def test_ml_model_cards_severity_rules_cover_each_axis_gap():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}
    severity_by_condition = {rule["when"]: rule["severity"] for rule in rules}

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions
    assert severity_by_condition["axis=intended_use AND coverage=gap"] == "high"
    assert severity_by_condition["axis=ethical_considerations AND coverage=gap"] == "high"
    assert severity_by_condition["axis=model_details AND coverage=gap"] == "medium"


def test_ml_model_cards_extension_contains_axes_and_classifications():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content
