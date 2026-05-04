"""Tests for codd validate --lexicon."""

import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.validator import validate_with_lexicon


def _valid_lexicon():
    return {
        "version": "1.0",
        "node_vocabulary": [
            {
                "id": "url_route",
                "description": "Browser-accessible route path",
                "naming_convention": "kebab-case",
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
        ],
        "failure_modes": [],
        "extractor_registry": {},
    }


def _write_lexicon(project_root, data):
    path = project_root / "project_lexicon.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_validate_with_lexicon_returns_empty_when_lexicon_missing(tmp_path):
    assert validate_with_lexicon(tmp_path) == []


def test_validate_with_lexicon_returns_empty_for_valid_lexicon(tmp_path):
    _write_lexicon(tmp_path, _valid_lexicon())

    assert validate_with_lexicon(tmp_path) == []


def test_validate_with_lexicon_reports_unknown_naming_convention(tmp_path):
    data = _valid_lexicon()
    data["node_vocabulary"][0]["naming_convention"] = "unknown-case"
    _write_lexicon(tmp_path, data)

    violations = validate_with_lexicon(tmp_path)

    assert violations == [
        {
            "node_id": "url_route",
            "violation_type": "unknown_convention",
            "expected": ["kebab-case", "SCREAMING_SNAKE_CASE"],
            "actual": "unknown-case",
            "message": "naming_convention 'unknown-case' not defined in naming_conventions",
        }
    ]


def test_validate_lexicon_cli_reports_ok_for_valid_lexicon(tmp_path):
    _write_lexicon(tmp_path, _valid_lexicon())

    runner = CliRunner()
    result = runner.invoke(main, ["validate", "--lexicon", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "Lexicon validation: OK (no violations)" in result.output
