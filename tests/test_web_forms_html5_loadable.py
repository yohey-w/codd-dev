"""Loadability tests for the web_forms_html5 lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "web_forms_html5"
EXPECTED_KINDS = {
    "html_forms_input_element_gap",
    "html_forms_form_element_gap",
    "html_forms_validation_gap",
    "html_forms_autocomplete_gap",
    "html_forms_label_gap",
    "html_forms_fieldset_gap",
    "html_forms_inputmode_gap",
    "html_forms_submission_gap",
    "html_forms_file_upload_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_web_forms_html5_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "web_forms_html5"
    assert "HTML Forms Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_web_forms_html5_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "web_forms_html5"
    assert manifest["lexicon_name"] == "web_forms_html5"
    assert manifest["source_url"] == "https://html.spec.whatwg.org/multipage/forms.html"
    assert "HTML Living Standard" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 9
    titles = {item["title"] for item in manifest["references"]}
    assert "HTML Living Standard: Forms" in titles
    assert "HTML Living Standard: The input element" in titles


def test_web_forms_html5_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_web_forms_html5_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
