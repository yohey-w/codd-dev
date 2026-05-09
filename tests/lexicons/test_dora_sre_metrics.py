"""Loadability and coverage-axis tests for the dora_sre_metrics lexicon."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "dora_sre_metrics"
EXPECTED_AXES = {
    "deployment_frequency",
    "lead_time_for_changes",
    "change_failure_rate",
    "mean_time_to_restore",
    "slo_sli_definition",
    "error_budget_policy",
    "toil_reduction",
    "incident_management",
}
EXPECTED_KINDS = {f"{axis}_gap" for axis in EXPECTED_AXES}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_dora_sre_metrics_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "dora_sre_metrics"
    assert "DORA Metrics and SRE Principles Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )


def test_dora_sre_metrics_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "dora_sre_metrics"
    assert manifest["lexicon_name"] == "dora_sre_metrics"
    assert manifest["standard"] == "DORA Accelerate 2018 / Google SRE 2016"
    assert manifest["domain"] == "cross_industry"
    assert manifest["observation_dimensions"] == 8
    titles = {item["title"] for item in manifest["references"]}
    assert "DORA research program" in titles
    assert "Google SRE Book - Service Level Objectives" in titles


def test_dora_sre_metrics_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_dora_sre_metrics_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_dora_sre_metrics_axes_have_rationale_criticality_and_variants():
    for axis in _yaml("lexicon.yaml")["coverage_axes"]:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_dora_sre_metrics_variant_ids_are_dora_and_sre_literals():
    variant_ids = {
        variant["id"]
        for axis in _yaml("lexicon.yaml")["coverage_axes"]
        for variant in axis["variants"]
    }

    for expected in (
        "deployment frequency",
        "lead time for changes",
        "change failure rate",
        "time to restore service",
        "service level indicator",
        "service level objective",
        "error budget",
        "toil",
        "incident management",
    ):
        assert expected in variant_ids


def test_dora_sre_metrics_severity_rules_cover_each_axis_gap():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}
    severity_by_condition = {rule["when"]: rule["severity"] for rule in rules}

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions
    assert severity_by_condition["axis=slo_sli_definition AND coverage=gap"] == "high"
    assert severity_by_condition["axis=incident_management AND coverage=gap"] == "high"
    assert severity_by_condition["axis=deployment_frequency AND coverage=gap"] == "medium"


def test_dora_sre_metrics_extension_contains_axes_and_classifications():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content
