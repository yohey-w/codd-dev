"""Tests for Coherence Engine detector adapters."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import yaml

import codd.drift as drift_module
import codd.validator as validator_module
from codd.coherence_adapters import (
    design_token_violation_to_event,
    drift_entry_to_event,
    validation_issue_to_event,
)
from codd.coherence_engine import DriftEvent, EventBus
from codd.drift import DriftEntry, compute_drift
from codd.validator import (
    DesignTokenViolation,
    ValidationIssue,
    validate_design_tokens,
    validate_with_lexicon,
)


def _valid_lexicon():
    return {
        "version": "1.0",
        "node_vocabulary": [
            {
                "id": "url_route",
                "description": "Browser route path",
                "naming_convention": "unknown-case",
            }
        ],
        "naming_conventions": [
            {"id": "kebab-case", "regex": "^[a-z][a-z0-9-]*$"},
        ],
        "design_principles": [],
        "failure_modes": [],
        "extractor_registry": {},
    }


@dataclass(frozen=True)
class _Token:
    id: str
    value: str


@dataclass(frozen=True)
class _ExtractResult:
    tokens: list[_Token]
    error: str | None = None


def _install_design_md(monkeypatch, tokens: list[_Token]) -> None:
    module = types.ModuleType("codd.design_md")

    class DesignMdExtractor:
        def extract(self, path):
            return _ExtractResult(tokens=tokens)

    module.DesignMdExtractor = DesignMdExtractor
    monkeypatch.setitem(sys.modules, "codd.design_md", module)


def test_drift_entry_to_event_dict():
    event = drift_entry_to_event(
        {
            "description": "Route only appears in docs",
            "type": "design-only",
            "path": "/admin",
            "expected": "/admin",
            "actual": "",
        }
    )

    assert isinstance(event, DriftEvent)
    assert event.kind == "drift"
    assert event.payload["description"] == "Route only appears in docs"
    assert event.payload["drift_type"] == "design-only"
    assert event.payload["location"] == "/admin"


def test_drift_entry_to_event_object():
    event = drift_entry_to_event(
        DriftEntry(kind="impl-only", url="/api/health", source="implementation", closest_match="/health")
    )

    assert event.payload["drift_type"] == "impl-only"
    assert event.payload["location"] == "/api/health"
    assert event.payload["after"] == "/health"
    assert event.severity == "amber"


def test_drift_to_event_publishes():
    bus = EventBus()
    event = drift_entry_to_event({"description": "drift", "location": "DESIGN.md"}, bus=bus)

    assert bus.published_events() == [event]


def test_drift_to_event_no_bus():
    event = drift_entry_to_event(object(), bus=None)

    assert isinstance(event, DriftEvent)
    assert event.payload["description"]


def test_validation_issue_error():
    issue = ValidationIssue(level="ERROR", code="invalid_node_id", location="docs/a.md", message="bad node")

    event = validation_issue_to_event(issue)

    assert event.kind == "lexicon_violation"
    assert event.severity == "red"
    assert event.fix_strategy == "auto"
    assert event.payload["rule"] == "invalid_node_id"


def test_validation_issue_warning():
    issue = ValidationIssue(level="WARNING", code="dangling_convention", location="docs/a.md", message="warn")

    event = validation_issue_to_event(issue)

    assert event.severity == "amber"
    assert event.fix_strategy == "hitl"
    assert event.payload["location"] == "docs/a.md"


def test_validation_issue_publishes():
    bus = EventBus()
    event = validation_issue_to_event(
        {"level": "error", "message": "bad lexicon", "violation_type": "unknown_convention"},
        bus=bus,
    )

    assert bus.published_events() == [event]


def test_design_token_violation():
    violation = DesignTokenViolation(file="App.tsx", line=3, pattern="#1a73e8", suggestion="colors.Primary")

    event = design_token_violation_to_event(violation)

    assert event.kind == "design_token_drift"
    assert event.payload["file"] == "App.tsx"
    assert event.payload["token"] == "#1a73e8"
    assert event.payload["expected_value"] == "colors.Primary"


def test_adapter_payload_structure():
    event = drift_entry_to_event({"description": "route drift", "location": "docs/routes.md", "extra": "kept"})

    assert event.payload["description"] == "route drift"
    assert event.payload["location"] == "docs/routes.md"
    assert event.payload["extra"] == "kept"


def test_unknown_type_fallback():
    event = validation_issue_to_event(12345)

    assert isinstance(event, DriftEvent)
    assert event.payload["description"] == "12345"
    assert event.severity == "amber"


def test_compute_drift_publishes_with_opt_in_bus(monkeypatch):
    bus = EventBus()
    monkeypatch.setattr(drift_module, "_coherence_bus", bus)

    result = compute_drift(["/old"], ["/new"])

    assert len(result.drift) == 2
    assert [event.kind for event in bus.published_events()] == ["drift", "drift"]


def test_validate_with_lexicon_publishes_with_opt_in_bus(tmp_path, monkeypatch):
    bus = EventBus()
    monkeypatch.setattr(validator_module, "_coherence_bus", bus)
    (tmp_path / "project_lexicon.yaml").write_text(yaml.safe_dump(_valid_lexicon()), encoding="utf-8")

    violations = validate_with_lexicon(tmp_path)

    assert len(violations) == 1
    assert bus.published_events()[0].kind == "lexicon_violation"
    assert bus.published_events()[0].payload["rule"] == "unknown_convention"


def test_validate_design_tokens_publishes_with_opt_in_bus(tmp_path, monkeypatch):
    bus = EventBus()
    monkeypatch.setattr(validator_module, "_coherence_bus", bus)
    _install_design_md(monkeypatch, [_Token("colors.Primary", "#1A73E8")])
    (tmp_path / "DESIGN.md").write_text("# Tokens\n", encoding="utf-8")
    (tmp_path / "App.tsx").write_text("const style = { color: '#1A73E8' };\n", encoding="utf-8")

    violations = validate_design_tokens(tmp_path)

    assert len(violations) == 1
    assert bus.published_events()[0].kind == "design_token_drift"
