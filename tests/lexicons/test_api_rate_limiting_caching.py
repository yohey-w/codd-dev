"""Loadability and coverage-axis tests for api_rate_limiting_caching."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "api_rate_limiting_caching"
EXPECTED_AXES = {
    "rate_limit_strategy",
    "quota_management",
    "throttling_response",
    "cache_control_headers",
    "etag_conditional_requests",
    "cdn_edge_caching",
    "idempotency_keys",
}
EXPECTED_KINDS = {f"{axis}_gap" for axis in EXPECTED_AXES}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_api_rate_limiting_caching_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "api_rate_limiting_caching"
    assert "API Rate Limiting and Caching Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )


def test_api_rate_limiting_caching_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "api_rate_limiting_caching"
    assert manifest["lexicon_name"] == "api_rate_limiting_caching"
    assert manifest["standard"] == "RFC 6585 / RFC 7234 / IETF"
    assert manifest["domain"] == "cross_industry"
    assert manifest["observation_dimensions"] == 7
    titles = {item["title"] for item in manifest["references"]}
    assert "RFC 6585 - Additional HTTP Status Codes" in titles
    assert "RFC 7234 - HTTP/1.1 Caching" in titles


def test_api_rate_limiting_caching_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_api_rate_limiting_caching_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_api_rate_limiting_caching_axes_have_rationale_criticality_and_variants():
    for axis in _yaml("lexicon.yaml")["coverage_axes"]:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_api_rate_limiting_caching_variant_ids_are_http_literals():
    variant_ids = {
        variant["id"]
        for axis in _yaml("lexicon.yaml")["coverage_axes"]
        for variant in axis["variants"]
    }

    for expected in (
        "rate limit",
        "quota",
        "429",
        "Retry-After",
        "Cache-Control",
        "max-age",
        "no-store",
        "ETag",
        "If-None-Match",
        "CDN",
        "Idempotency-Key",
    ):
        assert expected in variant_ids


def test_api_rate_limiting_caching_severity_rules_cover_each_axis_gap():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}
    severity_by_condition = {rule["when"]: rule["severity"] for rule in rules}

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions
    assert severity_by_condition["axis=rate_limit_strategy AND coverage=gap"] == "high"
    assert severity_by_condition["axis=throttling_response AND coverage=gap"] == "medium"
    assert severity_by_condition["axis=idempotency_keys AND coverage=gap"] == "high"


def test_api_rate_limiting_caching_extension_contains_axes_and_classifications():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content
