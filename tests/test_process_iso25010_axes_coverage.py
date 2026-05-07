"""Coverage-axis tests for the process_iso25010 lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "process_iso25010"
EXPECTED_AXES = {
    "functional_suitability",
    "performance_efficiency",
    "compatibility",
    "usability",
    "reliability",
    "security",
    "maintainability",
    "portability",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_process_iso25010_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "process_iso25010"
    assert "ISO/IEC 25010 Product Quality Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == {
        "functional_suitability_gap",
        "performance_efficiency_gap",
        "compatibility_gap",
        "usability_gap",
        "reliability_gap",
        "security_quality_gap",
        "maintainability_gap",
        "portability_gap",
    }


def test_process_iso25010_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_process_iso25010_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_process_iso25010_axes_have_rationale_criticality_and_variants():
    for axis in _yaml("lexicon.yaml")["coverage_axes"]:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_process_iso25010_variant_ids_are_quality_literals():
    variant_ids = {
        variant["id"]
        for axis in _yaml("lexicon.yaml")["coverage_axes"]
        for variant in axis["variants"]
    }

    for expected in (
        "Functional Suitability",
        "Performance Efficiency",
        "Compatibility",
        "Usability",
        "Reliability",
        "Security",
        "Maintainability",
        "Portability",
        "Functional correctness",
        "Availability",
        "Testability",
    ):
        assert expected in variant_ids


def test_process_iso25010_severity_rules_and_extension_cover_each_axis():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rule_conditions = {rule["when"] for rule in _yaml("severity_rules.yaml")["rules"]}
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions
        assert f"`{axis}`" in content
    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content

