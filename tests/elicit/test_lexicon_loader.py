"""Tests for generic elicitation lexicon loading."""

from pathlib import Path

import pytest
import yaml

from codd.elicit.lexicon_loader import LexiconLoadError, load_lexicon


def _write_lexicon(tmp_path: Path, manifest: dict | None = None) -> Path:
    root = tmp_path / "lexicons" / "sample"
    root.mkdir(parents=True)
    base = tmp_path / "codd" / "elicit" / "templates"
    base.mkdir(parents=True)
    (base / "elicit_prompt_L0.md").write_text("BASE PROMPT\n", encoding="utf-8")
    (root / "elicit_extend.md").write_text(
        "---\nextends: codd/elicit/templates/elicit_prompt_L0.md\n---\n"
        "EXTENSION BODY\n",
        encoding="utf-8",
    )
    (root / "recommended_kinds.yaml").write_text(
        yaml.safe_dump({"recommended_kinds": ["first_kind", "second_kind"]}),
        encoding="utf-8",
    )
    data = {
        "lexicon_name": "sample",
        "extends": "codd/elicit/templates/elicit_prompt_L0.md",
        "prompt_extension": "elicit_extend.md",
        "recommended_kinds": "recommended_kinds.yaml",
    }
    if manifest:
        data.update(manifest)
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )
    return root


def test_load_lexicon_from_directory_combines_base_prompt_and_extension(tmp_path):
    root = _write_lexicon(tmp_path)

    config = load_lexicon(root)

    assert config.lexicon_name == "sample"
    assert config.prompt_extension_content.startswith("BASE PROMPT")
    assert "EXTENSION BODY" in config.prompt_extension_content
    assert config.recommended_kinds == ["first_kind", "second_kind"]


def test_load_lexicon_from_manifest_file(tmp_path):
    root = _write_lexicon(tmp_path)

    config = load_lexicon(root / "manifest.yaml")

    assert config.lexicon_name == "sample"


def test_resolves_extends_from_ancestor_of_manifest_directory(tmp_path):
    root = _write_lexicon(tmp_path)

    config = load_lexicon(root)

    assert "BASE PROMPT" in config.prompt_extension_content


def test_uses_prompt_frontmatter_extends_when_manifest_omits_extends(tmp_path):
    root = _write_lexicon(tmp_path, {"extends": None})
    data = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    data.pop("extends")
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )

    config = load_lexicon(root)

    assert config.prompt_extension_content.startswith("BASE PROMPT")


def test_strips_prompt_extension_frontmatter_from_loaded_content(tmp_path):
    root = _write_lexicon(tmp_path)

    config = load_lexicon(root)

    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
    assert "---" not in config.prompt_extension_content


def test_missing_manifest_raises_lexicon_error(tmp_path):
    with pytest.raises(LexiconLoadError, match="manifest not found"):
        load_lexicon(tmp_path / "missing")


def test_manifest_must_be_yaml_mapping(tmp_path):
    root = tmp_path / "lexicon"
    root.mkdir()
    (root / "manifest.yaml").write_text("- not-a-mapping\n", encoding="utf-8")

    with pytest.raises(LexiconLoadError, match="must contain a mapping"):
        load_lexicon(root)


def test_missing_required_manifest_field_raises_lexicon_error(tmp_path):
    root = _write_lexicon(tmp_path)
    data = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    data.pop("prompt_extension")
    (root / "manifest.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(LexiconLoadError, match="prompt_extension"):
        load_lexicon(root)


def test_missing_prompt_extension_file_raises_lexicon_error(tmp_path):
    root = _write_lexicon(tmp_path, {"prompt_extension": "missing.md"})

    with pytest.raises(LexiconLoadError, match="prompt_extension path not found"):
        load_lexicon(root)


def test_missing_recommended_kinds_key_raises_lexicon_error(tmp_path):
    root = _write_lexicon(tmp_path)
    (root / "recommended_kinds.yaml").write_text("other: []\n", encoding="utf-8")

    with pytest.raises(LexiconLoadError, match="recommended_kinds"):
        load_lexicon(root)


def test_recommended_kinds_entries_must_be_strings(tmp_path):
    root = _write_lexicon(tmp_path)
    (root / "recommended_kinds.yaml").write_text(
        yaml.safe_dump({"recommended_kinds": ["valid", 123]}),
        encoding="utf-8",
    )

    with pytest.raises(LexiconLoadError, match="non-empty strings"):
        load_lexicon(root)


def test_duplicate_recommended_kinds_raise_lexicon_error(tmp_path):
    root = _write_lexicon(tmp_path)
    (root / "recommended_kinds.yaml").write_text(
        yaml.safe_dump({"recommended_kinds": ["same", "same"]}),
        encoding="utf-8",
    )

    with pytest.raises(LexiconLoadError, match="duplicate"):
        load_lexicon(root)
