from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import get_args

import pytest

from codd.elicit import Finding, FindingDimension, FindingType, Severity


def test_finding_is_dataclass() -> None:
    assert is_dataclass(Finding)


def test_finding_declares_expected_fields() -> None:
    assert [field.name for field in fields(Finding)] == [
        "id",
        "kind",
        "severity",
        "name",
        "question",
        "details",
        "related_requirement_ids",
        "source",
        "rationale",
    ]


def test_severity_literal_values_are_fixed() -> None:
    assert set(get_args(Severity)) == {"critical", "high", "medium", "amber", "info"}


def test_process_user_journey_finding_type_is_declared() -> None:
    assert FindingType.MISSING_JOURNEY_FOR_ACTOR.value == "missing_journey_for_actor"
    assert FindingDimension.PROCESS_USER_JOURNEY.value == "process_user_journey"


def test_defaults_are_empty_and_greenfield() -> None:
    finding = Finding(id="F-1", kind="gap", severity="info")

    assert finding.name is None
    assert finding.question is None
    assert finding.details == {}
    assert finding.related_requirement_ids == []
    assert finding.source == "greenfield"
    assert finding.rationale == ""


def test_default_collections_are_not_shared() -> None:
    first = Finding(id="F-1", kind="gap", severity="info")
    second = Finding(id="F-2", kind="gap", severity="info")

    first.details["key"] = "value"
    first.related_requirement_ids.append("REQ-1")

    assert second.details == {}
    assert second.related_requirement_ids == []


def test_to_dict_returns_full_payload() -> None:
    finding = Finding(
        id="F-1",
        kind="gap",
        severity="medium",
        name="Name",
        question="Question?",
        details={"evidence": "missing"},
        related_requirement_ids=["REQ-1"],
        rationale="Reason",
    )

    assert finding.to_dict() == {
        "id": "F-1",
        "kind": "gap",
        "severity": "medium",
        "name": "Name",
        "question": "Question?",
        "details": {"evidence": "missing"},
        "related_requirement_ids": ["REQ-1"],
        "source": "greenfield",
        "rationale": "Reason",
    }


def test_from_dict_accepts_minimal_payload() -> None:
    finding = Finding.from_dict({"id": "F-1", "kind": "gap", "severity": "high"})

    assert finding == Finding(id="F-1", kind="gap", severity="high")


def test_from_dict_accepts_amber_severity_and_actor_dimension() -> None:
    finding = Finding.from_dict(
        {
            "id": "F-1",
            "kind": "missing_journey_for_actor",
            "severity": "amber",
            "details": {"actor": "Operator", "dimension": "process_user_journey"},
        }
    )

    assert finding.severity == "amber"
    assert finding.actor == "Operator"
    assert finding.dimension == "process_user_journey"


def test_from_dict_trims_required_text() -> None:
    finding = Finding.from_dict({"id": " F-1 ", "kind": " gap ", "severity": " info "})

    assert finding.id == "F-1"
    assert finding.kind == "gap"
    assert finding.severity == "info"


def test_from_dict_rejects_missing_required_values() -> None:
    with pytest.raises(ValueError, match="id"):
        Finding.from_dict({"kind": "gap", "severity": "info"})
    with pytest.raises(ValueError, match="kind"):
        Finding.from_dict({"id": "F-1", "severity": "info"})


def test_from_dict_rejects_invalid_shapes() -> None:
    with pytest.raises(ValueError, match="mapping"):
        Finding.from_dict([])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="details"):
        Finding.from_dict({"id": "F-1", "kind": "gap", "severity": "info", "details": []})
    with pytest.raises(ValueError, match="related_requirement_ids"):
        Finding.from_dict({"id": "F-1", "kind": "gap", "severity": "info", "related_requirement_ids": {}})
