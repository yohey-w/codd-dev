"""Coverage-axis tests for the ops_iac_terraform lexicon plug-in."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "ops_iac_terraform"
EXPECTED_AXES = {
    "terraform",
    "provider",
    "resource",
    "data",
    "variable",
    "state",
    "module",
    "workspace",
    "backend",
    "Sentinel",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_ops_iac_terraform_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_ops_iac_terraform_axes_have_rationale_criticality_and_variants():
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    for axis in axes:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_ops_iac_terraform_variant_ids_are_terraform_literals():
    variant_ids = {
        variant["id"]
        for axis in _yaml("lexicon.yaml")["coverage_axes"]
        for variant in axis["variants"]
    }

    for expected in (
        "terraform",
        "required_version",
        "required_providers",
        "provider",
        "alias",
        "resource",
        "count",
        "for_each",
        "depends_on",
        "lifecycle",
        "data",
        "data resource",
        "variable",
        "output",
        "sensitive",
        "state",
        "terraform.tfstate",
        "moved",
        "import",
        "module",
        "source",
        "version",
        "workspace",
        "terraform.workspace",
        "backend",
        "terraform init",
        "Sentinel",
        "policy set",
        "enforcement level",
        "tfplan",
        "tfconfig",
        "tfstate",
        "tfrun",
    ):
        assert expected in variant_ids


def test_ops_iac_terraform_severity_rules_cover_each_axis_gap():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions


def test_ops_iac_terraform_extension_contains_coverage_examples():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content
