"""Coverage and loadability tests for the i18n_unicode_cldr lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "i18n_unicode_cldr"
EXPECTED_AXES = {
    "locale_tagging",
    "character_encoding",
    "time_zone_handling",
    "number_currency_format",
    "date_time_calendar",
    "text_collation",
    "rtl_bidi_support",
    "pluralization_rules",
    "translation_string_management",
}
EXPECTED_KINDS = {
    "locale_tagging_gap",
    "character_encoding_gap",
    "time_zone_handling_gap",
    "number_currency_format_gap",
    "date_time_calendar_gap",
    "text_collation_gap",
    "rtl_bidi_support_gap",
    "pluralization_rules_gap",
    "translation_string_management_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_i18n_unicode_cldr_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "i18n_unicode_cldr"
    assert "Internationalization Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_i18n_unicode_cldr_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["id"] == "i18n_unicode_cldr"
    assert manifest["lexicon_name"] == "i18n_unicode_cldr"
    assert manifest["standard"] == "Unicode CLDR 44 / IETF BCP 47 / ICU"
    assert manifest["domain"] == "cross_industry"
    assert manifest["observation_dimensions"] == 9
    titles = {item["title"] for item in manifest["references"]}
    assert "CLDR Specifications" in titles
    assert "Unicode Extensions for BCP 47" in titles


def test_i18n_unicode_cldr_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_i18n_unicode_cldr_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_i18n_unicode_cldr_axes_have_rationale_criticality_and_variants():
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    for axis in axes:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}
            assert variant["attributes"]["source_literal"]


def test_i18n_unicode_cldr_severity_rules_cover_each_axis_gap():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions

    severity_by_condition = {rule["when"]: rule["severity"] for rule in rules}
    assert severity_by_condition["axis=character_encoding AND coverage=gap"] == "critical"
    assert severity_by_condition["axis=locale_tagging AND coverage=gap"] == "high"
    assert severity_by_condition["axis=rtl_bidi_support AND coverage=gap"] == "medium"


def test_i18n_unicode_cldr_extension_contains_coverage_examples():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content


def test_i18n_unicode_cldr_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
