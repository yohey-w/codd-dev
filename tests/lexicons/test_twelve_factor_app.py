"""Coverage and loadability tests for the twelve_factor_app lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "twelve_factor_app"
EXPECTED_AXES = {
    "codebase",
    "dependencies",
    "config",
    "backing_services",
    "build_release_run",
    "processes",
    "port_binding",
    "concurrency",
    "disposability",
    "dev_prod_parity",
    "logs",
    "admin_processes",
}
EXPECTED_KINDS = {
    "codebase_gap",
    "dependencies_gap",
    "config_gap",
    "backing_services_gap",
    "build_release_run_gap",
    "processes_gap",
    "port_binding_gap",
    "concurrency_gap",
    "disposability_gap",
    "dev_prod_parity_gap",
    "logs_gap",
    "admin_processes_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_twelve_factor_app_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "twelve_factor_app"
    assert "Twelve-Factor App Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_twelve_factor_app_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["id"] == "twelve_factor_app"
    assert manifest["lexicon_name"] == "twelve_factor_app"
    assert manifest["standard"] == "12factor.net"
    assert manifest["domain"] == "cross_industry"
    assert manifest["source_url"] == "https://12factor.net/"
    assert manifest["observation_dimensions"] == 12
    titles = {item["title"] for item in manifest["references"]}
    assert "The Twelve-Factor App" in titles
    assert "III. Config" in titles


def test_twelve_factor_app_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_twelve_factor_app_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_twelve_factor_app_axes_have_rationale_criticality_and_variants():
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


def test_twelve_factor_app_severity_rules_cover_each_axis_gap():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions

    severity_by_condition = {rule["when"]: rule["severity"] for rule in rules}
    assert severity_by_condition["axis=config AND coverage=gap"] == "critical"
    assert severity_by_condition["axis=dependencies AND coverage=gap"] == "high"
    assert severity_by_condition["axis=dev_prod_parity AND coverage=gap"] == "medium"


def test_twelve_factor_app_extension_contains_coverage_examples():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content


def test_twelve_factor_app_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
