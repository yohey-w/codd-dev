"""Adapters that convert existing detector outputs to DriftEvent."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from codd.coherence_engine import DriftEvent, EventBus


def _as_mapping(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "__dict__"):
        return vars(value)
    return {"raw": str(value)}


def drift_entry_to_event(entry: Any, bus: EventBus | None = None) -> DriftEvent:
    """Convert a DriftEntry-like object to DriftEvent and optionally publish."""
    data = _as_mapping(entry)
    payload = {
        "description": data.get("description", data.get("reason", data.get("raw", str(data)))),
        "drift_type": data.get("drift_type", data.get("type", data.get("kind", "unknown"))),
        "location": data.get("location", data.get("path", data.get("url", ""))),
        "before": data.get("before", data.get("expected", "")),
        "after": data.get("after", data.get("actual", data.get("closest_match", ""))),
        **{
            key: value
            for key, value in data.items()
            if key
            not in (
                "description",
                "reason",
                "drift_type",
                "type",
                "location",
                "path",
                "url",
                "before",
                "expected",
                "after",
                "actual",
                "closest_match",
            )
        },
    }
    event = DriftEvent(
        source_artifact="design_doc",
        target_artifact="implementation",
        change_type="modified",
        payload=payload,
        severity="amber",
        fix_strategy="hitl",
        kind="drift",
    )
    if bus is not None:
        bus.publish(event)
    return event


def validation_issue_to_event(issue: Any, bus: EventBus | None = None) -> DriftEvent:
    """Convert a ValidationIssue-like object to DriftEvent and optionally publish."""
    data = _as_mapping(issue)
    level = data.get("level", data.get("severity", data.get("violation_type", "warning")))
    severity_map = {
        "error": "red",
        "blocked": "amber",
        "warning": "amber",
        "warn": "amber",
        "info": "green",
        "ok": "green",
    }
    severity = severity_map.get(str(level).lower(), "amber")
    payload = {
        "description": data.get("description", data.get("message", data.get("raw", str(data)))),
        "rule": data.get(
            "rule",
            data.get("check", data.get("code", data.get("violation_type", ""))),
        ),
        "location": data.get("location", data.get("file", data.get("node_id", ""))),
        "expected": data.get("expected", ""),
        "actual": data.get("actual", data.get("found", "")),
        **{
            key: value
            for key, value in data.items()
            if key
            not in (
                "description",
                "message",
                "rule",
                "check",
                "code",
                "violation_type",
                "location",
                "file",
                "node_id",
                "expected",
                "actual",
                "found",
                "level",
                "severity",
            )
        },
    }
    event = DriftEvent(
        source_artifact="lexicon",
        target_artifact="implementation",
        change_type="modified",
        payload=payload,
        severity=severity,
        fix_strategy="auto" if severity == "red" else "hitl",
        kind="lexicon_violation",
    )
    if bus is not None:
        bus.publish(event)
    return event


def design_token_violation_to_event(violation: Any, bus: EventBus | None = None) -> DriftEvent:
    """Convert a design-token violation to DriftEvent and optionally publish."""
    data = _as_mapping(violation)
    event = DriftEvent(
        source_artifact="design_md",
        target_artifact="implementation",
        change_type="modified",
        payload={
            "description": data.get("description", data.get("message", data.get("raw", str(data)))),
            "token": data.get("token", data.get("property", data.get("pattern", ""))),
            "expected_value": data.get(
                "expected_value",
                data.get("expected", data.get("suggestion", "")),
            ),
            "actual_value": data.get("actual_value", data.get("actual", data.get("pattern", ""))),
            "file": data.get("file", data.get("path", "")),
            **{key: value for key, value in data.items() if key not in ("description", "message")},
        },
        severity="amber",
        fix_strategy="hitl",
        kind="design_token_drift",
    )
    if bus is not None:
        bus.publish(event)
    return event
