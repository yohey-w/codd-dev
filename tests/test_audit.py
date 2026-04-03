"""Tests for codd audit — consolidated change review pack."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from codd.audit import (
    AuditResult,
    _determine_verdict,
    format_audit_json,
    format_audit_text,
    run_audit,
)
from codd.reviewer import ReviewIssue, ReviewResult, ReviewSummary
from codd.validator import ValidationIssue, ValidationResult


def _make_validation(*, errors: int = 0, warnings: int = 0, docs: int = 5) -> ValidationResult:
    issues = []
    for i in range(errors):
        issues.append(ValidationIssue(level="ERROR", code="test_err", location=f"doc{i}.md", message=f"Error {i}"))
    for i in range(warnings):
        issues.append(ValidationIssue(level="WARNING", code="test_warn", location=f"doc{i}.md", message=f"Warning {i}"))
    result = ValidationResult(documents_checked=docs, issues=issues)
    return result


def _make_review(*, pass_count: int = 3, fail_count: int = 0, critical: bool = False) -> ReviewSummary:
    results = []
    for i in range(pass_count):
        results.append(ReviewResult(
            node_id=f"design:mod{i}", path=f"docs/mod{i}.md", title=f"mod{i}",
            verdict="PASS", score=85, issues=[], feedback="Good.",
        ))
    for i in range(fail_count):
        issues = []
        if critical:
            issues.append(ReviewIssue(severity="CRITICAL", message="Critical issue"))
        else:
            issues.append(ReviewIssue(severity="WARNING", message="Minor issue"))
        results.append(ReviewResult(
            node_id=f"design:fail{i}", path=f"docs/fail{i}.md", title=f"fail{i}",
            verdict="FAIL", score=55, issues=issues, feedback="Needs work.",
        ))
    total = pass_count + fail_count
    avg = sum(r.score for r in results) / total if total else 0
    return ReviewSummary(results=results, pass_count=pass_count, fail_count=fail_count, avg_score=avg)


class TestDetermineVerdict:
    def test_approve_when_all_clean(self):
        v = _make_validation()
        r = _make_review()
        assert _determine_verdict(v, {}, [], r) == "APPROVE"

    def test_reject_on_validation_errors(self):
        v = _make_validation(errors=2)
        assert _determine_verdict(v, {}, [], None) == "REJECT"

    def test_reject_on_critical_review(self):
        v = _make_validation()
        r = _make_review(fail_count=1, critical=True)
        assert _determine_verdict(v, {}, [], r) == "REJECT"

    def test_conditional_on_convention_alerts(self):
        v = _make_validation()
        r = _make_review()
        alerts = [{"source": "a", "target": "b", "rule": "must_review"}]
        assert _determine_verdict(v, {}, alerts, r) == "CONDITIONAL"

    def test_conditional_on_review_fail_non_critical(self):
        v = _make_validation()
        r = _make_review(fail_count=1, critical=False)
        assert _determine_verdict(v, {}, [], r) == "CONDITIONAL"

    def test_conditional_on_validation_warnings(self):
        v = _make_validation(warnings=1)
        r = _make_review()
        assert _determine_verdict(v, {}, [], r) == "CONDITIONAL"

    def test_approve_without_review(self):
        v = _make_validation()
        assert _determine_verdict(v, {}, [], None) == "APPROVE"


class TestFormatAuditText:
    def test_contains_verdict(self):
        result = AuditResult(
            timestamp="2026-04-04T00:00:00Z",
            diff_target="HEAD",
            changed_files=["src/foo.py"],
            validation=_make_validation(),
            impact_nodes={},
            convention_alerts=[],
            review=None,
            verdict="APPROVE",
        )
        text = format_audit_text(result)
        assert "APPROVE" in text
        assert "Changed Files (1)" in text
        assert "src/foo.py" in text

    def test_contains_review_skipped(self):
        result = AuditResult(
            timestamp="2026-04-04T00:00:00Z",
            diff_target="HEAD",
            changed_files=[],
            validation=_make_validation(),
            impact_nodes={},
            convention_alerts=[],
            review=None,
            verdict="APPROVE",
        )
        text = format_audit_text(result)
        assert "SKIPPED" in text


class TestFormatAuditJson:
    def test_valid_json(self):
        result = AuditResult(
            timestamp="2026-04-04T00:00:00Z",
            diff_target="HEAD",
            changed_files=["a.py"],
            validation=_make_validation(warnings=1),
            impact_nodes={"design:foo": {"depth": 1, "confidence": 0.8, "source": "a.py"}},
            convention_alerts=[],
            review=_make_review(),
            verdict="CONDITIONAL",
        )
        raw = format_audit_json(result)
        data = json.loads(raw)
        assert data["verdict"] == "CONDITIONAL"
        assert data["risk_level"] == "MEDIUM"
        assert data["impact"]["affected_nodes"] == 1
        assert data["review"]["pass_count"] == 3
        assert data["validation"]["warnings"] == 1

    def test_json_without_review(self):
        result = AuditResult(
            timestamp="2026-04-04T00:00:00Z",
            diff_target="HEAD",
            changed_files=[],
            validation=_make_validation(),
            impact_nodes={},
            convention_alerts=[],
            review=None,
            verdict="APPROVE",
        )
        data = json.loads(format_audit_json(result))
        assert data["review"] is None
        assert data["action_required"] == "Safe to merge. No issues found."


class TestAuditResultProperties:
    def test_risk_level(self):
        base = dict(
            timestamp="", diff_target="", changed_files=[],
            validation=_make_validation(), impact_nodes={},
            convention_alerts=[], review=None,
        )
        assert AuditResult(**base, verdict="APPROVE").risk_level == "LOW"
        assert AuditResult(**base, verdict="CONDITIONAL").risk_level == "MEDIUM"
        assert AuditResult(**base, verdict="REJECT").risk_level == "HIGH"
