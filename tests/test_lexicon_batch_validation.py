"""Batch validation reporting for required_artifacts (cmd_466 #4)."""

from __future__ import annotations

import pytest

from codd.lexicon import LexiconError, validate_lexicon


def _base_data() -> dict:
    return {
        "version": "1.0",
        "project": "test",
        "scope": "system_implementation",
        "phase": "mvp",
        "node_vocabulary": [
            {
                "id": "x",
                "description": "x role",
                "naming_convention": "snake_case",
                "provenance": "human",
            }
        ],
        "naming_conventions": [{"id": "snake_case", "regex": "^[a-z_]+$"}],
        "design_principles": [],
        "required_artifacts": [],
    }


def test_single_missing_field_still_reports():
    data = _base_data()
    data["required_artifacts"] = [
        {"id": "design:x", "scope": "Scope", "source": "user_override"}  # no title
    ]
    with pytest.raises(LexiconError, match="missing required field 'title'"):
        validate_lexicon(data)


def test_multiple_missing_fields_reported_in_single_error():
    data = _base_data()
    data["required_artifacts"] = [
        {"id": "design:x"},  # missing title, scope, source
    ]
    with pytest.raises(LexiconError) as excinfo:
        validate_lexicon(data)
    text = str(excinfo.value)
    assert "title" in text
    assert "scope" in text
    assert "source" in text
    # Aggregated header
    assert "validation issue" in text


def test_multiple_artifacts_each_with_missing_fields_reported_once():
    data = _base_data()
    data["required_artifacts"] = [
        {"id": "design:a"},
        {"id": "design:b", "title": "B"},
    ]
    with pytest.raises(LexiconError) as excinfo:
        validate_lexicon(data)
    text = str(excinfo.value)
    assert "design:a" in text
    assert "design:b" in text


def test_invalid_source_value_aggregated_with_missing_fields():
    data = _base_data()
    data["required_artifacts"] = [
        {"id": "design:x", "source": "made_up"},  # source invalid + title/scope missing
    ]
    with pytest.raises(LexiconError) as excinfo:
        validate_lexicon(data)
    text = str(excinfo.value)
    assert "title" in text
    assert "scope" in text
    assert "source must be one of" in text
