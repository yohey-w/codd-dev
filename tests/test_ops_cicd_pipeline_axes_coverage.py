"""Coverage-axis tests for the ops_cicd_pipeline lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "ops_cicd_pipeline"
EXPECTED_AXES = {
    "declarative_config",
    "version_control",
    "automated_apply",
    "continuous_reconciliation",
    "drift_detection",
    "rollback",
    "observability",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_ops_cicd_pipeline_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "ops_cicd_pipeline"
    assert "CI/CD Pipeline GitOps Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == {
        "declarative_config_gap",
        "version_control_gap",
        "automated_apply_gap",
        "continuous_reconciliation_gap",
        "drift_detection_gap",
        "rollback_gap",
        "pipeline_observability_gap",
    }


def test_ops_cicd_pipeline_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_ops_cicd_pipeline_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_ops_cicd_pipeline_axes_have_rationale_criticality_and_variants():
    for axis in _yaml("lexicon.yaml")["coverage_axes"]:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_ops_cicd_pipeline_variant_ids_are_gitops_literals():
    variant_ids = {
        variant["id"]
        for axis in _yaml("lexicon.yaml")["coverage_axes"]
        for variant in axis["variants"]
    }

    for expected in (
        "Declarative",
        "Versioned and Immutable",
        "Pulled Automatically",
        "Continuously Reconciled",
        "drift",
        "rollback",
        "sync status",
    ):
        assert expected in variant_ids


def test_ops_cicd_pipeline_severity_rules_and_extension_cover_each_axis():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rule_conditions = {rule["when"] for rule in _yaml("severity_rules.yaml")["rules"]}
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions
        assert f"`{axis}`" in content
    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content

