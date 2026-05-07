"""Tests for the BABOK elicitation lexicon plug-in."""

from pathlib import Path

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[2]
BABOK_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "babok"
EXPECTED_KINDS = {
    "axis_candidate",
    "spec_hole",
    "risk_observation",
    "constraint_implicit",
    "acceptance_unclear",
    "stakeholder_unidentified",
    "kpi_undefined",
    "rule_unclear",
}
DIMENSIONS = [
    "stakeholder",
    "goal",
    "flow",
    "issue",
    "data",
    "functional",
    "non-functional",
    "rule",
    "constraint",
    "acceptance",
    "risk",
    "assumption",
    "term",
]


def test_babok_manifest_loads_as_lexicon_config():
    config = load_lexicon(BABOK_ROOT)

    assert config.lexicon_name == "babok"
    assert "BABOK 13 Observation Dimensions" in config.prompt_extension_content


def test_babok_recommended_kinds_contains_all_expected_values():
    config = load_lexicon(BABOK_ROOT)

    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_babok_recommended_kinds_count_is_eight():
    config = load_lexicon(BABOK_ROOT)

    assert len(config.recommended_kinds) == 8


def test_babok_extension_contains_all_thirteen_dimensions():
    content = (BABOK_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for dimension in DIMENSIONS:
        assert f"`{dimension}`" in content


def test_babok_extension_instructs_explicit_and_omission_checks():
    content = (BABOK_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    assert "明示的記述があるか" in content
    assert "抜け漏れがないか" in content
    assert "findings" in content


def test_babok_extension_declares_base_prompt_in_frontmatter():
    content = (BABOK_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    assert content.startswith("---\n")
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" in content


def test_babok_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(BABOK_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )


def test_core_loader_does_not_name_the_babok_plugin_or_its_kinds():
    source = (REPO_ROOT / "codd" / "elicit" / "lexicon_loader.py").read_text(
        encoding="utf-8"
    )

    assert "babok" not in source.lower()
    for kind in EXPECTED_KINDS:
        assert kind not in source
