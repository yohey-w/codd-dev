"""Coverage-axis tests for the compliance_iso27001 lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "compliance_iso27001"
EXPECTED_AXES = {
    "context_organization",
    "leadership",
    "planning",
    "support",
    "operation",
    "performance_evaluation",
    "improvement",
    "risk_treatment_plan",
    "SOA",
    "access_control",
    "cryptography",
    "physical_security",
    "supplier_relationships",
    "incident_management",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_compliance_iso27001_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "compliance_iso27001"
    assert "ISO/IEC 27001 Compliance Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == {
        "context_organization_gap",
        "leadership_gap",
        "planning_gap",
        "support_gap",
        "operation_gap",
        "performance_evaluation_gap",
        "improvement_gap",
        "risk_treatment_plan_gap",
        "statement_of_applicability_gap",
        "access_control_gap",
        "cryptography_gap",
        "physical_security_gap",
        "supplier_relationships_gap",
        "incident_management_gap",
    }


def test_compliance_iso27001_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_compliance_iso27001_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_compliance_iso27001_axes_have_rationale_criticality_and_variants():
    for axis in _yaml("lexicon.yaml")["coverage_axes"]:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_compliance_iso27001_variant_ids_are_iso_literals():
    variant_ids = {
        variant["id"]
        for axis in _yaml("lexicon.yaml")["coverage_axes"]
        for variant in axis["variants"]
    }

    for expected in (
        "Context of the organization",
        "Leadership",
        "Planning",
        "Support",
        "Operation",
        "Performance evaluation",
        "Improvement",
        "information security risk treatment plan",
        "Statement of Applicability",
        "Access control",
        "Cryptography",
        "Physical security",
        "Supplier relationships",
        "Information security incident management",
    ):
        assert expected in variant_ids


def test_compliance_iso27001_severity_rules_and_extension_cover_each_axis():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rule_conditions = {rule["when"] for rule in _yaml("severity_rules.yaml")["rules"]}
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions
        assert f"`{axis}`" in content
    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content

