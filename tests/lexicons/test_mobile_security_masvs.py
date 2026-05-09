"""Tests for the OWASP MASVS mobile security lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "mobile_security_masvs"
EXPECTED_AXES = {
    "storage_security",
    "crypto_best_practices",
    "auth_session_management",
    "network_communication",
    "platform_interaction",
    "code_quality",
    "resilience",
}
EXPECTED_KINDS = {
    "mobile_storage_security_gap",
    "mobile_crypto_best_practices_gap",
    "mobile_auth_session_management_gap",
    "mobile_network_communication_gap",
    "mobile_platform_interaction_gap",
    "mobile_code_quality_gap",
    "mobile_resilience_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_mobile_security_masvs_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "mobile_security_masvs"
    assert "OWASP MASVS Mobile Security Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_mobile_security_masvs_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["id"] == "mobile_security_masvs"
    assert manifest["name"] == "Mobile Security (OWASP MASVS v2)"
    assert manifest["lexicon_name"] == "mobile_security_masvs"
    assert manifest["version"] == "1.0.0"
    assert manifest["standard"] == "OWASP MASVS v2.x"
    assert manifest["domain"] == "cross_industry"
    assert manifest["source_url"] == "https://mas.owasp.org/MASVS/"
    assert manifest["observation_dimensions"] == 7
    assert manifest["references"]


def test_mobile_security_masvs_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_mobile_security_masvs_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_mobile_security_masvs_axes_have_rationale_criticality_and_variants():
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    for axis in axes:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_mobile_security_masvs_severity_rules_cover_each_axis_gap():
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}

    for axis in EXPECTED_AXES:
        assert f"axis={axis} AND coverage=gap" in rule_conditions


def test_mobile_security_masvs_required_not_found_severity_rules():
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_by_condition = {rule["when"]: rule for rule in rules}

    assert (
        rule_by_condition["axis=storage_security AND coverage=not_found"]["severity"]
        == "high"
    )
    assert (
        rule_by_condition["axis=network_communication AND coverage=not_found"][
            "severity"
        ]
        == "critical"
    )
    assert (
        rule_by_condition["axis=auth_session_management AND coverage=not_found"][
            "severity"
        ]
        == "high"
    )


def test_mobile_security_masvs_extension_contains_coverage_examples():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap", "not_found"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content
