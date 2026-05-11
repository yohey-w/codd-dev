"""Unit tests for codd.fix.risk_classifier."""

from __future__ import annotations

import json

import pytest

from codd.fix.risk_classifier import classify_risk


def test_empty_diff_is_not_risky():
    result = classify_risk("")
    assert not result.risky
    assert result.categories == []


def test_schema_migration_detected():
    diff = (
        "--- /dev/null\n"
        "+++ b/prisma/migrations/20260101_add_table.sql\n"
        "+CREATE TABLE users (id INT);\n"
    )
    result = classify_risk(diff)
    assert result.risky
    assert "schema_migration" in result.categories


def test_dependency_add_detected():
    diff = (
        "--- a/pyproject.toml\n"
        "+++ b/pyproject.toml\n"
        "@@\n"
        "+\"requests\",\n"
    )
    result = classify_risk(diff)
    assert "dependency_add" in result.categories
    assert result.risky


def test_mass_deletion_detected():
    diff = "--- a/x.py\n+++ b/x.py\n@@\n" + ("-removed\n" * 50)
    result = classify_risk(diff)
    assert "mass_deletion" in result.categories
    assert result.risky


def test_test_removal_detected():
    diff = (
        "--- a/tests/test_foo.py\n"
        "+++ b/tests/test_foo.py\n"
        "@@\n"
        "-def test_old_case():\n"
        "-    pass\n"
    )
    result = classify_risk(diff)
    assert "test_removal" in result.categories
    assert result.risky


def test_safe_diff_returns_not_risky():
    diff = (
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@\n"
        "+a single new line of docs\n"
    )
    result = classify_risk(diff)
    assert not result.risky
    assert result.categories == []


def test_llm_can_upgrade_risk():
    """If heuristics see nothing but LLM says risky, still report risky."""
    diff = "--- a/x.md\n+++ b/x.md\n@@\n+benign change\n"

    def llm(_prompt: str) -> str:
        return json.dumps({
            "risky": True,
            "categories": ["config_change"],
            "summary": "policy text changed",
        })

    result = classify_risk(diff, ai_invoke=llm)
    assert result.risky
    assert "config_change" in result.categories


def test_llm_failure_falls_back_to_heuristic():
    diff = (
        "--- /dev/null\n"
        "+++ b/prisma/migrations/x.sql\n"
        "+CREATE TABLE t (id INT);\n"
    )

    def bad_llm(_prompt: str) -> str:
        raise RuntimeError("ai unavailable")

    result = classify_risk(diff, ai_invoke=bad_llm)
    assert result.risky
    assert "schema_migration" in result.categories
