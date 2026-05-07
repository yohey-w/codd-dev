"""Finding model for elicit discovery results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, get_args


Severity = Literal["critical", "high", "medium", "info"]
Source = Literal["greenfield", "extract_brownfield"]

_SEVERITIES = set(get_args(Severity))
_SOURCES = set(get_args(Source))


@dataclass
class Finding:
    """A generic discovery result.

    ``kind`` is intentionally open-ended. Lexicon plug-ins may suggest values,
    but the core model only persists and displays the string it receives.
    """

    id: str
    kind: str
    severity: Severity
    name: str | None = None
    question: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    related_requirement_ids: list[str] = field(default_factory=list)
    source: Source = "greenfield"
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Finding":
        if not isinstance(payload, dict):
            raise ValueError("Finding payload must be a mapping")

        finding_id = str(payload.get("id", "")).strip()
        kind = str(payload.get("kind", "")).strip()
        severity = str(payload.get("severity", "")).strip()
        if not finding_id:
            raise ValueError("Finding id is required")
        if not kind:
            raise ValueError("Finding kind is required")
        if severity not in _SEVERITIES:
            raise ValueError(f"Finding severity must be one of {sorted(_SEVERITIES)}")

        source = str(payload.get("source", "greenfield")).strip() or "greenfield"
        if source not in _SOURCES:
            raise ValueError(f"Finding source must be one of {sorted(_SOURCES)}")

        details = payload.get("details") or {}
        if not isinstance(details, dict):
            raise ValueError("Finding details must be a mapping")

        related = payload.get("related_requirement_ids") or []
        if not isinstance(related, list):
            raise ValueError("Finding related_requirement_ids must be a list")

        return cls(
            id=finding_id,
            kind=kind,
            severity=severity,  # type: ignore[arg-type]
            name=_optional_text(payload.get("name")),
            question=_optional_text(payload.get("question")),
            details=details,
            related_requirement_ids=[str(item) for item in related],
            source=source,  # type: ignore[arg-type]
            rationale=str(payload.get("rationale") or ""),
        )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
