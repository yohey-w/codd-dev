"""Coverage-axis tests for the process_test_iso29119 lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "process_test_iso29119"
EXPECTED_AXES = {
    "test_concepts_definitions",
    "test_processes",
    "test_documentation",
    "test_techniques",
    "keyword_driven_testing",
    "work_aided_software_testing",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_process_test_iso29119_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "process_test_iso29119"
    assert "ISO/IEC/IEEE 29119 Software Testing Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == {
        "test_concepts_definitions_gap",
        "test_processes_gap",
        "test_documentation_gap",
        "test_techniques_gap",
        "keyword_driven_testing_gap",
        "work_aided_software_testing_gap",
    }


def test_process_test_iso29119_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_process_test_iso29119_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_process_test_iso29119_axes_have_rationale_criticality_and_variants():
    for axis in _yaml("lexicon.yaml")["coverage_axes"]:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_process_test_iso29119_variant_ids_are_testing_literals():
    variant_ids = {
        variant["id"]
        for axis in _yaml("lexicon.yaml")["coverage_axes"]
        for variant in axis["variants"]
    }

    for expected in (
        "general concepts",
        "test processes",
        "organizational level",
        "test management level",
        "dynamic test levels",
        "test documentation",
        "test design techniques",
        "keyword-driven testing",
        "hierarchical keywords",
        "work products",
        "work product reviews",
    ):
        assert expected in variant_ids


def test_process_test_iso29119_severity_rules_and_extension_cover_each_axis():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rule_conditions = {rule["when"] for rule in _yaml("severity_rules.yaml")["rules"]}
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions
        assert f"`{axis}`" in content
    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content

