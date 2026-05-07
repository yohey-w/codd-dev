"""Coverage-axis tests for the mobile_a11y_native lexicon plug-in."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "mobile_a11y_native"
EXPECTED_AXES = {
    "touchscreens",
    "small screen sizes",
    "different input modalities",
    "color contrast",
    "larger text sizes",
    "screen reader",
    "haptics",
    "Reduce Motion",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_mobile_a11y_native_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_mobile_a11y_native_axes_have_rationale_criticality_and_variants():
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    for axis in axes:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_mobile_a11y_native_variant_ids_are_mobile_accessibility_literals():
    variant_ids = {
        variant["id"]
        for axis in _yaml("lexicon.yaml")["coverage_axes"]
        for variant in axis["variants"]
    }

    for expected in (
        "touchscreens",
        "48dp",
        "44x44 pt",
        "small screen sizes",
        "different input modalities",
        "speech",
        "color contrast",
        "4.5:1",
        "Dynamic Type",
        "VoiceOver",
        "TalkBack",
        "contentDescription",
        "haptics",
        "Reduce Motion",
    ):
        assert expected in variant_ids


def test_mobile_a11y_native_severity_rules_cover_each_axis_gap():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions


def test_mobile_a11y_native_extension_contains_coverage_examples():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content
