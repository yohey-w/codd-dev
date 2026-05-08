from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from codd.elicit.engine import ElicitEngine


class FakeAiCommand:
    def __init__(self, findings: list[dict[str, Any]]):
        self.output = json.dumps({"findings": findings})

    def invoke(self, prompt: str) -> str:
        return self.output


AXES = [
    {"axis_type": "goal", "concern": "business"},
    {"axis_type": "flow", "concern": "system"},
    {"axis_type": "issue", "concern": "both"},
]

MVP_RULES = {
    "rules": [
        {
            "when": "concern=business AND phase=mvp AND severity=high",
            "severity": "info",
        }
    ]
}


def _write_project_lexicon(root: Path, *, scope: str | None = None, phase: str | None = None) -> None:
    payload: dict[str, Any] = {
        "version": "1.0",
        "node_vocabulary": [{"id": "entity", "description": "Domain entity"}],
        "naming_conventions": [],
        "design_principles": [],
    }
    if scope is not None:
        payload["scope"] = scope
    if phase is not None:
        payload["phase"] = phase
    (root / "project_lexicon.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _finding(finding_id: str, axis: str, severity: str = "high") -> dict[str, Any]:
    return {
        "id": finding_id,
        "kind": "coverage_gap",
        "severity": severity,
        "details": {"dimension": axis},
    }


def _run(root: Path, findings: list[dict[str, Any]], *, rules: dict[str, Any] | None = None):
    return ElicitEngine(ai_command=FakeAiCommand(findings)).run(
        root,
        lexicon_config={"coverage_axes": AXES, "severity_rules": rules or {}},
    )


def test_scope_system_implementation_filters_business_axes(tmp_path: Path) -> None:
    _write_project_lexicon(tmp_path, scope="system_implementation")

    result = _run(
        tmp_path,
        [_finding("F-goal", "goal"), _finding("F-flow", "flow"), _finding("F-issue", "issue")],
    )

    assert [finding.id for finding in result.findings] == ["F-flow", "F-issue"]


def test_scope_full_includes_all(tmp_path: Path) -> None:
    _write_project_lexicon(tmp_path, scope="full")

    result = _run(tmp_path, [_finding("F-goal", "goal"), _finding("F-flow", "flow")])

    assert [finding.id for finding in result.findings] == ["F-goal", "F-flow"]


def test_scope_business_only_includes_business(tmp_path: Path) -> None:
    _write_project_lexicon(tmp_path, scope="business_only")

    result = _run(
        tmp_path,
        [_finding("F-goal", "goal"), _finding("F-flow", "flow"), _finding("F-issue", "issue")],
    )

    assert [finding.id for finding in result.findings] == ["F-goal", "F-issue"]


def test_phase_mvp_demotes_business_high_to_info(tmp_path: Path) -> None:
    _write_project_lexicon(tmp_path, scope="full", phase="mvp")

    result = _run(
        tmp_path,
        [_finding("F-goal", "goal"), _finding("F-flow", "flow")],
        rules=MVP_RULES,
    )

    severities = {finding.id: finding.severity for finding in result.findings}
    assert severities == {"F-goal": "info", "F-flow": "high"}


def test_phase_production_no_severity_change(tmp_path: Path) -> None:
    _write_project_lexicon(tmp_path, scope="full", phase="production")

    result = _run(
        tmp_path,
        [_finding("F-goal", "goal"), _finding("F-flow", "flow")],
        rules=MVP_RULES,
    )

    assert {finding.id: finding.severity for finding in result.findings} == {
        "F-goal": "high",
        "F-flow": "high",
    }


def test_default_scope_system_implementation_filters_business_dimensions(
    tmp_path: Path,
) -> None:
    """cmd_455: omitting `scope:` defaults to system_implementation.

    Business-concern dimensions (goal) drop out of findings unless the
    project explicitly opts into `scope: full` or `scope: business_only`.
    """

    _write_project_lexicon(tmp_path)

    result = _run(tmp_path, [_finding("F-goal", "goal"), _finding("F-flow", "flow")])

    assert [finding.id for finding in result.findings] == ["F-flow"]
    assert {finding.severity for finding in result.findings} == {"high"}


def test_explicit_scope_full_overrides_new_default(tmp_path: Path) -> None:
    """cmd_455 backward compat: `scope: full` keeps the legacy behaviour."""

    _write_project_lexicon(tmp_path, scope="full")

    result = _run(tmp_path, [_finding("F-goal", "goal"), _finding("F-flow", "flow")])

    assert [finding.id for finding in result.findings] == ["F-goal", "F-flow"]
