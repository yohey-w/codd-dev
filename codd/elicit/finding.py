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
        raw_severity = str(payload.get("severity", "")).strip()
        if not finding_id:
            raise ValueError("Finding id is required")
        if not kind:
            raise ValueError("Finding kind is required")
        severity = _coerce_severity(raw_severity)

        source = str(payload.get("source", "greenfield")).strip() or "greenfield"
        if source not in _SOURCES:
            raise ValueError(f"Finding source must be one of {sorted(_SOURCES)}")

        details = payload.get("details")
        if details is None:
            details = {}
        if not isinstance(details, dict):
            raise ValueError("Finding details must be a mapping")

        related = payload.get("related_requirement_ids")
        if related is None:
            related = []
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


_SEVERITY_ALIASES = {
    "blocker": "critical",
    "fatal": "critical",
    "severe": "critical",
    "urgent": "critical",
    "error": "high",
    "major": "high",
    "important": "high",
    "warn": "medium",
    "warning": "medium",
    "moderate": "medium",
    "minor": "info",
    "low": "info",
    "informational": "info",
    "note": "info",
    "trivial": "info",
}


def _coerce_severity(raw: str) -> str:
    """Map a raw severity string into the canonical Literal set, with a safe fallback.

    Strict membership is preserved for canonical values; common synonyms are mapped
    deterministically; anything unknown defaults to ``info`` so downstream tools stay
    operational rather than aborting on LLM drift. Generic mapping only — no stack /
    framework / domain literals.
    """
    cleaned = raw.lower().strip()
    if cleaned in _SEVERITIES:
        return cleaned
    if cleaned in _SEVERITY_ALIASES:
        return _SEVERITY_ALIASES[cleaned]
    return "info"


@dataclass
class ElicitResult:
    findings: list[Finding] = field(default_factory=list)
    all_covered: bool = False
    lexicon_coverage_report: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __iter__(self):
        return iter(self.findings)

    def __len__(self) -> int:
        return len(self.findings)

    def __getitem__(self, index: int) -> Finding:
        return self.findings[index]

    def __bool__(self) -> bool:
        return bool(self.findings)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return self.findings == other
        if not isinstance(other, ElicitResult):
            return NotImplemented
        return (
            self.findings == other.findings
            and self.all_covered == other.all_covered
            and self.lexicon_coverage_report == other.lexicon_coverage_report
            and self.metadata == other.metadata
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_covered": self.all_covered,
            "lexicon_coverage_report": self.lexicon_coverage_report,
            "findings": [f.to_dict() for f in self.findings],
            "metadata": self.metadata,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "ElicitResult":
        if isinstance(payload, list):
            findings = [Finding.from_dict(item) for item in payload]
            return cls(findings=findings)
        if not isinstance(payload, dict):
            raise ValueError("Elicit output must be a JSON array or object")
        raw_findings = payload.get("findings", [])
        if not isinstance(raw_findings, list):
            raise ValueError("Elicit output 'findings' must be a list")
        findings = [Finding.from_dict(item) for item in raw_findings]
        coverage_raw = payload.get("lexicon_coverage_report", {})
        coverage: dict[str, str] = {}
        if isinstance(coverage_raw, dict):
            coverage = {str(k): str(v) for k, v in coverage_raw.items()}
        all_covered_raw = payload.get("all_covered", False)
        all_covered = bool(all_covered_raw) if not findings else (
            bool(all_covered_raw) and not findings
        )
        metadata_raw = payload.get("metadata", {})
        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
        return cls(
            findings=findings,
            all_covered=all_covered,
            lexicon_coverage_report=coverage,
            metadata=metadata,
        )
