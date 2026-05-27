"""Coverage-obligation schema and status normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


class SourceType(str, Enum):
    """Authoritative source kinds that can produce an obligation."""

    DESIGN_DOC = "design_doc"
    REQUIREMENT = "requirement"
    LEXICON = "lexicon"
    RUNTIME = "runtime"
    STATIC = "static"
    MANUAL = "manual"


class ObligationKind(str, Enum):
    """Normalized obligation vocabulary."""

    ROLE_SEQUENCE = "role_sequence"
    ACTION_OUTCOME = "action_outcome"
    GLOBAL_ACTION = "global_action"
    BREAKPOINT_COVERAGE = "breakpoint_coverage"
    CRUD_FLOW = "crud_flow"
    CONNECTIVITY = "connectivity"
    PRESENTATION_LOCALE = "presentation_locale"
    AGGREGATION_POLICY = "aggregation_policy"
    RUNTIME_CAPABILITY = "runtime_capability"
    LOWER_LEVEL_CONTRACT = "lower_level_contract"


class RiskLevel(str, Enum):
    """Risk levels used by Coverage-Obligation Driven E2E."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class CoverageStatus(str, Enum):
    """The only normalized coverage statuses accepted by the model."""

    COVERED_BY_E2E = "covered_by_e2e"
    COVERED_BY_LOWER_TEST = "covered_by_lower_test"
    WAIVED_WITH_REASON_AND_EXPIRY = "waived_with_reason_and_expiry"
    UNCOVERED = "uncovered"


REQUIRED_OBLIGATION_FIELDS: tuple[str, ...] = (
    "obligation_id",
    "source",
    "kind",
    "actor",
    "goal",
    "preconditions",
    "expected_outcomes",
    "side_effects",
    "risk_level",
    "coverage_status",
    "covered_by",
    "waiver_reason",
    "waiver_expiry",
)


_STATUS_ALIASES: dict[str, CoverageStatus] = {
    "covered_by_e2e": CoverageStatus.COVERED_BY_E2E,
    "e2e": CoverageStatus.COVERED_BY_E2E,
    "runtime_e2e": CoverageStatus.COVERED_BY_E2E,
    "covered_by_lower_test": CoverageStatus.COVERED_BY_LOWER_TEST,
    "lower_test": CoverageStatus.COVERED_BY_LOWER_TEST,
    "lower_level_test": CoverageStatus.COVERED_BY_LOWER_TEST,
    "delegated_to_lower_test": CoverageStatus.COVERED_BY_LOWER_TEST,
    "waived_with_reason_and_expiry": CoverageStatus.WAIVED_WITH_REASON_AND_EXPIRY,
    "waived": CoverageStatus.WAIVED_WITH_REASON_AND_EXPIRY,
    "waiver": CoverageStatus.WAIVED_WITH_REASON_AND_EXPIRY,
    "uncovered": CoverageStatus.UNCOVERED,
    "missing": CoverageStatus.UNCOVERED,
    "none": CoverageStatus.UNCOVERED,
    "not_applicable": CoverageStatus.UNCOVERED,
    "opt_out": CoverageStatus.UNCOVERED,
    "skip": CoverageStatus.UNCOVERED,
    "skipped": CoverageStatus.UNCOVERED,
}

_SOURCE_TYPE_ALIASES: dict[str, SourceType] = {
    "design": SourceType.DESIGN_DOC,
    "design_doc": SourceType.DESIGN_DOC,
    "requirement": SourceType.REQUIREMENT,
    "requirements": SourceType.REQUIREMENT,
    "lexicon": SourceType.LEXICON,
    "runtime": SourceType.RUNTIME,
    "static": SourceType.STATIC,
    "manual": SourceType.MANUAL,
}


@dataclass(frozen=True)
class CoverageSource:
    """Earliest authoritative source that caused an obligation."""

    type: SourceType | str
    ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", _source_type(self.type))
        object.__setattr__(self, "ref", str(self.ref).strip())
        if not self.ref:
            raise ValueError("coverage obligation source.ref is required")


@dataclass(frozen=True)
class CoverageEvidence:
    """Evidence reference recorded in ``covered_by``."""

    type: str
    ref: str
    status: str | None = None
    skipped: bool = False
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", str(self.type or "verification_test").strip())
        object.__setattr__(self, "ref", str(self.ref or "").strip())
        object.__setattr__(self, "status", _optional_str(self.status))
        object.__setattr__(self, "skip_reason", _optional_str(self.skip_reason))

    @property
    def is_valid(self) -> bool:
        """Return True when this evidence can count toward coverage."""

        return bool(self.ref) and not self.is_skip

    @property
    def is_skip(self) -> bool:
        """Return True when the evidence records a skipped execution."""

        return (
            self.skipped
            or _is_skip_token(self.status)
            or _is_skip_token(self.skip_reason)
        )


@dataclass(frozen=True)
class GeneratedE2ECandidate:
    """Future TODO stub; candidates are not coverage evidence."""

    candidate_id: str
    covers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", str(self.candidate_id).strip())
        object.__setattr__(self, "covers", _string_tuple(self.covers))


@dataclass(frozen=True)
class SelectedE2ESuite:
    """Future TODO stub; selection manifests are not coverage evidence."""

    suite_id: str
    candidate_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "suite_id", str(self.suite_id).strip())
        object.__setattr__(self, "candidate_ids", _string_tuple(self.candidate_ids))


@dataclass(frozen=True)
class CoverageObligation:
    """Internal model for a single coverage obligation."""

    obligation_id: str
    source: CoverageSource | Mapping[str, Any]
    kind: ObligationKind | str
    actor: str
    goal: str
    preconditions: Sequence[Any] | str | None
    expected_outcomes: Sequence[Any] | str | None
    side_effects: Sequence[Any] | str | None
    risk_level: RiskLevel | str
    coverage_status: CoverageStatus | str | None
    covered_by: Sequence[CoverageEvidence | Mapping[str, Any] | str] | None
    waiver_reason: str | None
    waiver_expiry: date | datetime | str | None
    generated_e2e_candidates: Sequence[GeneratedE2ECandidate | Mapping[str, Any] | str] = field(
        default_factory=tuple
    )
    selected_e2e_suite: Sequence[SelectedE2ESuite | Mapping[str, Any] | str] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        source = _coverage_source(self.source)
        covered_by = tuple(_coverage_evidence(item) for item in (self.covered_by or ()))
        waiver_expiry = _parse_date(self.waiver_expiry)

        object.__setattr__(self, "obligation_id", str(self.obligation_id).strip())
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "kind", _obligation_kind(self.kind))
        object.__setattr__(self, "actor", str(self.actor).strip())
        object.__setattr__(self, "goal", str(self.goal).strip())
        object.__setattr__(self, "preconditions", _string_tuple(self.preconditions))
        object.__setattr__(self, "expected_outcomes", _string_tuple(self.expected_outcomes))
        object.__setattr__(self, "side_effects", _string_tuple(self.side_effects))
        object.__setattr__(self, "risk_level", _risk_level(self.risk_level))
        object.__setattr__(self, "covered_by", covered_by)
        object.__setattr__(self, "waiver_reason", _optional_str(self.waiver_reason))
        object.__setattr__(self, "waiver_expiry", waiver_expiry)
        object.__setattr__(
            self,
            "generated_e2e_candidates",
            tuple(_generated_candidate(item) for item in self.generated_e2e_candidates),
        )
        object.__setattr__(
            self,
            "selected_e2e_suite",
            tuple(_selected_suite(item) for item in self.selected_e2e_suite),
        )
        object.__setattr__(
            self,
            "coverage_status",
            normalize_coverage_status(
                self.coverage_status,
                covered_by=covered_by,
                waiver_reason=self.waiver_reason,
                waiver_expiry=waiver_expiry,
            ),
        )

        _require_non_empty("obligation_id", self.obligation_id)
        _require_non_empty("actor", self.actor)
        _require_non_empty("goal", self.goal)

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        today: date | datetime | str | None = None,
    ) -> "CoverageObligation":
        """Build an obligation from a schema mapping."""

        payload = raw.get("coverage_obligation") if "coverage_obligation" in raw else raw
        if not isinstance(payload, Mapping):
            raise TypeError("coverage_obligation payload must be a mapping")
        missing = [field_name for field_name in REQUIRED_OBLIGATION_FIELDS if field_name not in payload]
        if missing:
            raise ValueError("missing coverage obligation fields: " + ", ".join(missing))

        obligation = cls(
            obligation_id=payload["obligation_id"],
            source=payload["source"],
            kind=payload["kind"],
            actor=payload["actor"],
            goal=payload["goal"],
            preconditions=payload["preconditions"],
            expected_outcomes=payload["expected_outcomes"],
            side_effects=payload["side_effects"],
            risk_level=payload["risk_level"],
            coverage_status=payload["coverage_status"],
            covered_by=payload["covered_by"],
            waiver_reason=payload["waiver_reason"],
            waiver_expiry=payload["waiver_expiry"],
            generated_e2e_candidates=payload.get("generated_e2e_candidates", ()),
            selected_e2e_suite=payload.get("selected_e2e_suite", ()),
        )
        object.__setattr__(
            obligation,
            "coverage_status",
            effective_coverage_status(obligation, today=today),
        )
        return obligation

    def effective_status(self, *, today: date | datetime | str | None = None) -> CoverageStatus:
        """Return the conservative normalized status for this obligation."""

        return effective_coverage_status(self, today=today)

    def is_incomplete(self, *, today: date | datetime | str | None = None) -> bool:
        """Return True when the obligation is not currently validly covered."""

        return is_incomplete_coverage(self, today=today)


def coverage_obligation_from_mapping(
    raw: Mapping[str, Any],
    *,
    today: date | datetime | str | None = None,
) -> CoverageObligation:
    """Build an obligation from a schema mapping."""

    return CoverageObligation.from_mapping(raw, today=today)


def normalize_coverage_status(
    status: CoverageStatus | str | None,
    *,
    covered_by: Iterable[CoverageEvidence | Mapping[str, Any] | str] = (),
    waiver_reason: str | None = None,
    waiver_expiry: date | datetime | str | None = None,
    today: date | datetime | str | None = None,
) -> CoverageStatus:
    """Normalize raw status into one of the four supported coverage states."""

    normalized = _coverage_status(status)
    evidence = tuple(_coverage_evidence(item) for item in covered_by)

    if normalized == CoverageStatus.COVERED_BY_E2E:
        return normalized if _has_valid_evidence(evidence) else CoverageStatus.UNCOVERED
    if normalized == CoverageStatus.COVERED_BY_LOWER_TEST:
        return normalized if _has_valid_evidence(evidence) else CoverageStatus.UNCOVERED
    if normalized == CoverageStatus.WAIVED_WITH_REASON_AND_EXPIRY:
        if _has_valid_waiver(waiver_reason, waiver_expiry, today=today):
            return normalized
        return CoverageStatus.UNCOVERED
    return CoverageStatus.UNCOVERED


def effective_coverage_status(
    obligation: CoverageObligation,
    *,
    today: date | datetime | str | None = None,
) -> CoverageStatus:
    """Return effective coverage after skip and waiver checks."""

    return normalize_coverage_status(
        obligation.coverage_status,
        covered_by=obligation.covered_by,
        waiver_reason=obligation.waiver_reason,
        waiver_expiry=obligation.waiver_expiry,
        today=today,
    )


def is_incomplete_coverage(
    obligation_or_status: CoverageObligation | CoverageStatus | str | None,
    *,
    covered_by: Iterable[CoverageEvidence | Mapping[str, Any] | str] = (),
    waiver_reason: str | None = None,
    waiver_expiry: date | datetime | str | None = None,
    today: date | datetime | str | None = None,
) -> bool:
    """Return True for uncovered, skipped, implicit opt-out, or expired waiver."""

    if isinstance(obligation_or_status, CoverageObligation):
        status = effective_coverage_status(obligation_or_status, today=today)
    else:
        status = normalize_coverage_status(
            obligation_or_status,
            covered_by=covered_by,
            waiver_reason=waiver_reason,
            waiver_expiry=waiver_expiry,
            today=today,
        )
    return status == CoverageStatus.UNCOVERED


def _coverage_status(value: CoverageStatus | str | None) -> CoverageStatus:
    if isinstance(value, CoverageStatus):
        return value
    token = _normalize_token(value)
    if not token:
        return CoverageStatus.UNCOVERED
    return _STATUS_ALIASES.get(token, CoverageStatus.UNCOVERED)


def _source_type(value: SourceType | str) -> SourceType:
    if isinstance(value, SourceType):
        return value
    token = _normalize_token(value)
    try:
        return _SOURCE_TYPE_ALIASES[token]
    except KeyError as exc:
        raise ValueError(f"unsupported coverage source type: {value}") from exc


def _obligation_kind(value: ObligationKind | str) -> ObligationKind:
    if isinstance(value, ObligationKind):
        return value
    token = _normalize_token(value)
    try:
        return ObligationKind(token)
    except ValueError as exc:
        raise ValueError(f"unsupported coverage obligation kind: {value}") from exc


def _risk_level(value: RiskLevel | str) -> RiskLevel:
    if isinstance(value, RiskLevel):
        return value
    token = str(value or "").strip().upper()
    try:
        return RiskLevel(token)
    except ValueError as exc:
        raise ValueError(f"unsupported coverage risk level: {value}") from exc


def _coverage_source(raw: CoverageSource | Mapping[str, Any]) -> CoverageSource:
    if isinstance(raw, CoverageSource):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("coverage obligation source must be a mapping")
    return CoverageSource(type=raw.get("type"), ref=raw.get("ref", ""))


def _coverage_evidence(raw: CoverageEvidence | Mapping[str, Any] | str) -> CoverageEvidence:
    if isinstance(raw, CoverageEvidence):
        return raw
    if isinstance(raw, str):
        return CoverageEvidence(type="verification_test", ref=raw)
    if not isinstance(raw, Mapping):
        raise TypeError("coverage evidence must be a mapping or string")
    return CoverageEvidence(
        type=str(raw.get("type") or raw.get("kind") or "verification_test"),
        ref=str(raw.get("ref") or raw.get("path") or raw.get("id") or ""),
        status=_optional_str(raw.get("status")),
        skipped=_boolish(raw.get("skipped", False)),
        skip_reason=_optional_str(raw.get("skip_reason") or raw.get("reason")),
    )


def _generated_candidate(raw: GeneratedE2ECandidate | Mapping[str, Any] | str) -> GeneratedE2ECandidate:
    if isinstance(raw, GeneratedE2ECandidate):
        return raw
    if isinstance(raw, str):
        return GeneratedE2ECandidate(candidate_id=raw)
    if not isinstance(raw, Mapping):
        raise TypeError("generated_e2e_candidates items must be mappings or strings")
    return GeneratedE2ECandidate(
        candidate_id=str(raw.get("candidate_id") or raw.get("id") or ""),
        covers=_string_tuple(raw.get("covers", ())),
    )


def _selected_suite(raw: SelectedE2ESuite | Mapping[str, Any] | str) -> SelectedE2ESuite:
    if isinstance(raw, SelectedE2ESuite):
        return raw
    if isinstance(raw, str):
        return SelectedE2ESuite(suite_id=raw)
    if not isinstance(raw, Mapping):
        raise TypeError("selected_e2e_suite items must be mappings or strings")
    return SelectedE2ESuite(
        suite_id=str(raw.get("suite_id") or raw.get("id") or ""),
        candidate_ids=_string_tuple(raw.get("candidate_ids", ())),
    )


def _has_valid_evidence(evidence: Iterable[CoverageEvidence]) -> bool:
    items = tuple(evidence)
    return (
        bool(items)
        and not any(item.is_skip for item in items)
        and any(item.is_valid for item in items)
    )


def _has_valid_waiver(
    waiver_reason: str | None,
    waiver_expiry: date | datetime | str | None,
    *,
    today: date | datetime | str | None = None,
) -> bool:
    reason = _optional_str(waiver_reason)
    expiry = _parse_date(waiver_expiry)
    if reason is None or expiry is None:
        return False
    return expiry > _today(today)


def _parse_date(value: date | datetime | str | None) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _today(value: date | datetime | str | None = None) -> date:
    parsed = _parse_date(value)
    return parsed if parsed is not None else date.today()


def _string_tuple(value: Sequence[Any] | str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value if item is not None)


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _normalize_token(value) in {"1", "true", "yes", "y", "skip", "skipped"}


def _is_skip_token(value: Any) -> bool:
    return _normalize_token(value) in {"skip", "skipped"}


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _require_non_empty(field_name: str, value: str) -> None:
    if not value:
        raise ValueError(f"coverage obligation {field_name} is required")


__all__ = [
    "CoverageEvidence",
    "CoverageObligation",
    "CoverageSource",
    "CoverageStatus",
    "GeneratedE2ECandidate",
    "ObligationKind",
    "REQUIRED_OBLIGATION_FIELDS",
    "RiskLevel",
    "SelectedE2ESuite",
    "SourceType",
    "coverage_obligation_from_mapping",
    "effective_coverage_status",
    "is_incomplete_coverage",
    "normalize_coverage_status",
]
