"""Tests for project_lexicon.yaml loading and validation."""

import pytest
import yaml

from codd.lexicon import LexiconError, ProjectLexicon, load_lexicon, validate_lexicon


def _write_lexicon(tmp_path, data):
    path = tmp_path / "project_lexicon.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _valid_lexicon():
    return {
        "version": "1.0",
        "node_vocabulary": [
            {
                "id": "url_route",
                "description": "Browser-accessible route path",
                "extractor": "filesystem_routes",
                "naming_convention": "kebab-case",
                "prefix_rules": [
                    {"role": "member", "prefix": "/my"},
                ],
            },
            {
                "id": "env_var",
                "description": "Runtime configuration environment variable",
                "naming_convention": "SCREAMING_SNAKE_CASE",
            },
        ],
        "naming_conventions": [
            {"id": "kebab-case", "regex": "^[a-z][a-z0-9-]*$"},
            {"id": "SCREAMING_SNAKE_CASE", "regex": "^[A-Z][A-Z0-9_]*$"},
        ],
        "design_principles": [
            "Routes use stable role prefixes.",
            "Runtime configuration names are shared across env, yaml, and cli.",
        ],
        "failure_modes": [
            {"id": "case_drift", "pattern": "case mismatch", "detector": "lexicon_validate"},
        ],
        "extractor_registry": {
            "filesystem_routes": {
                "type": "codd.extractors.FileSystemRouteExtractor",
                "description": "Extract route paths from framework files.",
            }
        },
    }


def test_load_project_lexicon_and_access_fields(tmp_path):
    _write_lexicon(tmp_path, _valid_lexicon())

    lexicon = load_lexicon(tmp_path)

    assert lexicon is not None
    assert lexicon.node_vocabulary[0]["id"] == "url_route"
    assert lexicon.naming_conventions == {
        "kebab-case": "^[a-z][a-z0-9-]*$",
        "SCREAMING_SNAKE_CASE": "^[A-Z][A-Z0-9_]*$",
    }
    assert lexicon.design_principles[0] == "Routes use stable role prefixes."
    assert lexicon.failure_modes[0]["id"] == "case_drift"
    assert lexicon.extractor_registry["filesystem_routes"]["type"] == (
        "codd.extractors.FileSystemRouteExtractor"
    )


def test_load_lexicon_returns_none_when_file_missing(tmp_path):
    assert load_lexicon(tmp_path) is None


def test_missing_required_section_raises_lexicon_error():
    data = _valid_lexicon()
    data.pop("node_vocabulary")

    with pytest.raises(LexiconError, match="Missing required section: 'node_vocabulary'"):
        validate_lexicon(data)


def test_node_vocabulary_item_missing_required_field_raises_lexicon_error():
    data = _valid_lexicon()
    data["node_vocabulary"][0].pop("description")

    with pytest.raises(LexiconError, match="node_vocabulary item missing required field 'description'"):
        validate_lexicon(data)


def test_as_context_string_contains_node_id_description_and_prefix_rules(tmp_path):
    _write_lexicon(tmp_path, _valid_lexicon())
    lexicon = load_lexicon(tmp_path)

    context = lexicon.as_context_string()

    assert "**url_route**" in context
    assert "Browser-accessible route path" in context
    assert "prefix for member: /my" in context


def test_get_vocabulary_item_returns_match_or_none(tmp_path):
    _write_lexicon(tmp_path, _valid_lexicon())
    lexicon = load_lexicon(tmp_path)

    assert lexicon.get_vocabulary_item("env_var")["description"] == (
        "Runtime configuration environment variable"
    )
    assert lexicon.get_vocabulary_item("db_table") is None


def test_extractor_registry_defaults_to_empty_mapping():
    data = _valid_lexicon()
    data.pop("extractor_registry")

    validate_lexicon(data)
    assert ProjectLexicon(data).extractor_registry == {}


def test_extractor_registry_item_requires_type():
    data = _valid_lexicon()
    data["extractor_registry"]["filesystem_routes"].pop("type")

    with pytest.raises(LexiconError, match="extractor_registry item 'filesystem_routes'"):
        validate_lexicon(data)
