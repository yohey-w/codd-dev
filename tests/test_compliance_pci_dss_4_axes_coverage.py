"""Coverage axis tests for the compliance_pci_dss_4 lexicon plug-in."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "compliance_pci_dss_4"
EXPECTED_AXES = {
    "Install and Maintain Network Security Controls",
    "Protect Stored Account Data",
    "Maintain a Vulnerability Management Program",
    "Restrict Access to System Components and Cardholder Data by Business Need to Know",
    "Regularly Monitor and Test Networks",
    "Support Information Security with Organizational Policies and Programs",
    "Protect Cardholder Data with Strong Cryptography During Transmission Over Open, Public Networks",
    "Develop and Maintain Secure Systems and Software",
    "Personnel screening",
    "Restrict Physical Access to Cardholder Data",
    "Security incident response plan",
    "PCI DSS scope",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_compliance_pci_dss_4_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_compliance_pci_dss_4_axes_have_rationale_criticality_and_variants():
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    for axis in axes:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_compliance_pci_dss_4_severity_rules_cover_each_axis_gap():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions


def test_compliance_pci_dss_4_extension_contains_coverage_examples():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content
