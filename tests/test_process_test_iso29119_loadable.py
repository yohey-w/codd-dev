"""Loadability tests for the process_test_iso29119 lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "process_test_iso29119"


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_process_test_iso29119_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "process_test_iso29119"
    assert "ISO/IEC/IEEE 29119 Software Testing Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )


def test_process_test_iso29119_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "process_test_iso29119"
    assert manifest["lexicon_name"] == "process_test_iso29119"
    assert manifest["source_url"] == "https://www.iso.org/standard/45142.html"
    assert manifest["source_version"] == "ISO/IEC/IEEE 29119 software testing series"
    assert manifest["observation_dimensions"] == 6


def test_process_test_iso29119_recommended_kinds_load():
    config = load_lexicon(LEXICON_ROOT)

    assert "test_processes_gap" in set(config.recommended_kinds)
    assert "work_aided_software_testing_gap" in set(config.recommended_kinds)

