"""Loadability tests for the process_iso25010 lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "process_iso25010"


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_process_iso25010_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "process_iso25010"
    assert "ISO/IEC 25010 Product Quality Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )


def test_process_iso25010_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "process_iso25010"
    assert manifest["lexicon_name"] == "process_iso25010"
    assert manifest["source_url"] == (
        "https://iso25000.com/index.php/en/iso-25000-standards/iso-25010"
    )
    assert manifest["source_version"] == "ISO/IEC 25010:2011 product quality model"
    assert manifest["observation_dimensions"] == 8


def test_process_iso25010_recommended_kinds_load():
    config = load_lexicon(LEXICON_ROOT)

    assert "functional_suitability_gap" in set(config.recommended_kinds)
    assert "portability_gap" in set(config.recommended_kinds)

