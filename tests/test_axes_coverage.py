"""Coverage-axis tests for the OWASP web security lexicon."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "web_security_owasp"

TOP10_AXES = {
    "broken_access_control",
    "cryptographic_failures",
    "injection",
    "insecure_design",
    "security_misconfiguration",
    "vulnerable_and_outdated_components",
    "identification_and_authentication_failures",
    "software_and_data_integrity_failures",
    "security_logging_and_monitoring_failures",
    "server_side_request_forgery_ssrf",
}

ASVS_COMPLEMENT_AXES = {
    "authentication": "V2 Authentication",
    "session_management": "V3 Session Management",
    "input_validation": "V5.1 Input Validation",
    "output_encoding_and_injection_prevention": (
        "V5.3 Output Encoding and Injection Prevention"
    ),
}


def _axes() -> list[dict]:
    payload = yaml.safe_load(
        (LEXICON_ROOT / "lexicon.yaml").read_text(encoding="utf-8")
    )
    return payload["coverage_axes"]


def test_web_security_owasp_declares_fourteen_axes():
    assert len(_axes()) == 14


def test_web_security_owasp_includes_all_top10_axes():
    axis_types = {axis["axis_type"] for axis in _axes()}

    assert TOP10_AXES <= axis_types


def test_web_security_owasp_includes_four_asvs_complement_axes():
    axes_by_type = {axis["axis_type"]: axis for axis in _axes()}

    for axis_type, literal_label in ASVS_COMPLEMENT_AXES.items():
        axis = axes_by_type[axis_type]
        assert literal_label in axis["variants"][0]["label"]
        assert "OWASP ASVS 4.0.3" in axis["rationale"]


def test_web_security_owasp_axes_have_rationale_criticality_and_variants():
    for axis in _axes():
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        assert axis["variants"][0]["id"]
        assert axis["variants"][0]["label"]
        assert axis["variants"][0]["criticality"] in {
            "critical",
            "high",
            "medium",
            "info",
        }


def test_web_security_owasp_variant_ids_are_unique_and_source_derived():
    variant_ids = [
        variant["id"]
        for axis in _axes()
        for variant in axis["variants"]
    ]

    assert len(variant_ids) == len(set(variant_ids))
    assert all(variant_id.startswith(("a0", "a10", "v")) for variant_id in variant_ids)


def test_web_security_owasp_severity_rules_cover_every_gap_axis():
    rules = yaml.safe_load(
        (LEXICON_ROOT / "severity_rules.yaml").read_text(encoding="utf-8")
    )["rules"]
    rule_conditions = "\n".join(rule["when"] for rule in rules)

    for axis in _axes():
        assert f"axis={axis['axis_type']} AND coverage=gap" in rule_conditions
