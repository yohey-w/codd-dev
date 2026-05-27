"""Generate and select E2E candidates from coverage obligations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from codd.coverage_obligations import (
    CoverageEvidence,
    CoverageObligation,
    CoverageStatus,
    coverage_obligation_from_mapping,
    effective_coverage_status,
)


_RISK_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_RISK_WEIGHT: dict[str, int] = {"P0": 64, "P1": 16, "P2": 4, "P3": 1}
_HIGH_RISK = {"P0", "P1"}


@dataclass(frozen=True)
class GeneratedE2ECandidateRecord:
    """A candidate E2E test that may cover one or more obligations."""

    candidate_id: str
    obligation_ids: Sequence[str]
    actor: str
    journey_or_flow: str
    risk_level: str
    reason: str
    recommended_test_type: str
    status: str = "candidate"
    selected_reason: str | None = None
    reason_codes: Sequence[str] = field(default_factory=tuple)
    future_marker: Mapping[str, Any] = field(default_factory=dict)
    cost: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _clean_text(self.candidate_id))
        object.__setattr__(self, "obligation_ids", _string_tuple(self.obligation_ids))
        object.__setattr__(self, "actor", _clean_text(self.actor) or "system")
        object.__setattr__(self, "journey_or_flow", _clean_text(self.journey_or_flow) or "unspecified")
        object.__setattr__(self, "risk_level", _risk_value(self.risk_level))
        object.__setattr__(self, "reason", _clean_text(self.reason))
        object.__setattr__(self, "recommended_test_type", _clean_text(self.recommended_test_type))
        object.__setattr__(self, "status", _clean_text(self.status) or "candidate")
        object.__setattr__(self, "selected_reason", _optional_text(self.selected_reason))
        object.__setattr__(self, "reason_codes", _string_tuple(self.reason_codes))
        object.__setattr__(self, "future_marker", dict(self.future_marker or {}))
        object.__setattr__(self, "cost", float(self.cost or 1.0))

    @property
    def covers(self) -> tuple[str, ...]:
        """Compatibility alias for set-cover inputs."""

        return tuple(self.obligation_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "obligation_ids": list(self.obligation_ids),
            "actor": self.actor,
            "journey_or_flow": self.journey_or_flow,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "recommended_test_type": self.recommended_test_type,
            "status": self.status,
            "selected_reason": self.selected_reason,
            "reason_codes": list(self.reason_codes),
            "future_marker": dict(self.future_marker),
        }


@dataclass(frozen=True)
class SelectedE2ESuiteRecord:
    """A selected suite entry produced by deterministic set cover."""

    candidate_id: str
    obligation_ids: Sequence[str]
    actor: str
    journey_or_flow: str
    risk_level: str
    reason: str
    recommended_test_type: str
    status: str = "selected"
    selected_reason: str | None = None
    reason_codes: Sequence[str] = field(default_factory=tuple)
    future_marker: Mapping[str, Any] = field(default_factory=dict)
    selection_order: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", _clean_text(self.candidate_id))
        object.__setattr__(self, "obligation_ids", _string_tuple(self.obligation_ids))
        object.__setattr__(self, "actor", _clean_text(self.actor) or "system")
        object.__setattr__(self, "journey_or_flow", _clean_text(self.journey_or_flow) or "unspecified")
        object.__setattr__(self, "risk_level", _risk_value(self.risk_level))
        object.__setattr__(self, "reason", _clean_text(self.reason))
        object.__setattr__(self, "recommended_test_type", _clean_text(self.recommended_test_type))
        object.__setattr__(self, "status", _clean_text(self.status) or "selected")
        object.__setattr__(self, "selected_reason", _optional_text(self.selected_reason))
        object.__setattr__(self, "reason_codes", _string_tuple(self.reason_codes))
        object.__setattr__(self, "future_marker", dict(self.future_marker or {}))
        object.__setattr__(self, "selection_order", int(self.selection_order or 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "obligation_ids": list(self.obligation_ids),
            "actor": self.actor,
            "journey_or_flow": self.journey_or_flow,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "recommended_test_type": self.recommended_test_type,
            "status": self.status,
            "selected_reason": self.selected_reason,
            "reason_codes": list(self.reason_codes),
            "future_marker": dict(self.future_marker),
            "selection_order": self.selection_order,
        }


@dataclass(frozen=True)
class CoverageCandidateSelectionResult:
    """Complete core payload for candidate generation and suite selection."""

    generated_e2e_candidates: Sequence[GeneratedE2ECandidateRecord]
    selected_e2e_suite: Sequence[SelectedE2ESuiteRecord]
    unselected_e2e_candidates: Sequence[Mapping[str, Any]]
    excluded_obligations: Sequence[Mapping[str, Any]]
    required_obligation_ids: Sequence[str]
    uncovered_required_obligation_ids: Sequence[str]
    trace_matrix: Sequence[Mapping[str, Any]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_e2e_candidates",
            tuple(_candidate_record(candidate) for candidate in self.generated_e2e_candidates),
        )
        object.__setattr__(
            self,
            "selected_e2e_suite",
            tuple(_selected_record(candidate) for candidate in self.selected_e2e_suite),
        )
        object.__setattr__(
            self,
            "unselected_e2e_candidates",
            tuple(dict(item) for item in self.unselected_e2e_candidates),
        )
        object.__setattr__(self, "excluded_obligations", tuple(dict(item) for item in self.excluded_obligations))
        object.__setattr__(self, "required_obligation_ids", _string_tuple(self.required_obligation_ids))
        object.__setattr__(
            self,
            "uncovered_required_obligation_ids",
            _string_tuple(self.uncovered_required_obligation_ids),
        )
        object.__setattr__(self, "trace_matrix", tuple(dict(item) for item in self.trace_matrix))

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_e2e_candidates": [
                candidate.to_dict() for candidate in self.generated_e2e_candidates
            ],
            "selected_e2e_suite": [selected.to_dict() for selected in self.selected_e2e_suite],
            "unselected_e2e_candidates": [dict(item) for item in self.unselected_e2e_candidates],
            "excluded_obligations": [dict(item) for item in self.excluded_obligations],
            "required_obligation_ids": list(self.required_obligation_ids),
            "uncovered_required_obligation_ids": list(self.uncovered_required_obligation_ids),
            "trace_matrix": [dict(item) for item in self.trace_matrix],
        }


@dataclass(frozen=True)
class _ObligationView:
    obligation: CoverageObligation
    declared_status: str | None
    payload: Mapping[str, Any]
    metadata: Mapping[str, Any]


def generate_e2e_candidates(
    obligations: Iterable[CoverageObligation | Mapping[str, Any] | Any],
    *,
    today: date | datetime | str | None = None,
) -> tuple[GeneratedE2ECandidateRecord, ...]:
    """Return deterministic E2E candidates for obligations that require E2E attention."""

    views = _obligation_views(obligations, today=today)
    candidates: list[GeneratedE2ECandidateRecord] = []
    for view in sorted(views, key=_view_sort_key):
        exclusion = _exclusion_record(view, today=today)
        if exclusion is not None:
            continue
        candidates.append(_generated_candidate_for(view, today=today))
    return tuple(candidates)


def select_e2e_suite(
    candidates: Iterable[GeneratedE2ECandidateRecord | Mapping[str, Any]],
    obligations: Iterable[CoverageObligation | Mapping[str, Any] | Any],
    *,
    today: date | datetime | str | None = None,
    max_candidates: int | None = None,
) -> tuple[SelectedE2ESuiteRecord, ...]:
    """Select a deterministic small set-cover suite for required obligations."""

    candidate_records = tuple(sorted((_candidate_record(item) for item in candidates), key=_candidate_sort_key))
    views = _obligation_views(obligations, today=today)
    required_ids = set(_required_obligation_ids(views, today=today))
    if not required_ids:
        return ()

    risk_by_id = {view.obligation.obligation_id: _risk_value(view.obligation.risk_level) for view in views}
    remaining = set(required_ids)
    selected: list[SelectedE2ESuiteRecord] = []
    used_fingerprints: set[tuple[str, tuple[str, ...]]] = set()

    while remaining:
        ranked: list[tuple[tuple[Any, ...], GeneratedE2ECandidateRecord, set[str]]] = []
        for candidate in candidate_records:
            if candidate.status not in {"candidate", "generated"}:
                continue
            new_ids = set(candidate.obligation_ids) & remaining
            if not new_ids:
                continue
            fingerprint = _candidate_fingerprint(candidate)
            duplicate_cost = 1 if fingerprint in used_fingerprints else 0
            score = _selection_rank(candidate, new_ids, risk_by_id, duplicate_cost)
            ranked.append((score, candidate, new_ids))

        if not ranked:
            break

        ranked.sort(key=lambda item: item[0])
        _, chosen, newly_covered = ranked[0]
        selected.append(_selected_from_candidate(chosen, newly_covered, len(selected) + 1, risk_by_id))
        remaining.difference_update(newly_covered)
        used_fingerprints.add(_candidate_fingerprint(chosen))
        if max_candidates is not None and len(selected) >= max_candidates:
            break

    return tuple(selected)


def candidate_selection_payload(
    obligations: Iterable[CoverageObligation | Mapping[str, Any] | Any],
    *,
    candidates: Iterable[GeneratedE2ECandidateRecord | Mapping[str, Any]] | None = None,
    today: date | datetime | str | None = None,
    max_candidates: int | None = None,
) -> dict[str, Any]:
    """Return generated candidates, selected suite, exclusions, and trace rows."""

    obligation_items = tuple(obligations)
    views = _obligation_views(obligation_items, today=today)
    generated = (
        tuple(_candidate_record(item) for item in candidates)
        if candidates is not None
        else generate_e2e_candidates(obligation_items, today=today)
    )
    selected = select_e2e_suite(generated, obligation_items, today=today, max_candidates=max_candidates)
    required_ids = _required_obligation_ids(views, today=today)
    selected_ids = set().union(*(set(item.obligation_ids) for item in selected)) if selected else set()
    uncovered_ids = tuple(obligation_id for obligation_id in required_ids if obligation_id not in selected_ids)
    excluded = tuple(
        record for view in sorted(views, key=_view_sort_key) if (record := _exclusion_record(view, today=today))
    )
    unselected = _unselected_records(generated, selected, selected_ids)
    trace_rows = _trace_rows(views, generated, selected, excluded, today=today)
    result = CoverageCandidateSelectionResult(
        generated_e2e_candidates=generated,
        selected_e2e_suite=selected,
        unselected_e2e_candidates=unselected,
        excluded_obligations=excluded,
        required_obligation_ids=required_ids,
        uncovered_required_obligation_ids=uncovered_ids,
        trace_matrix=trace_rows,
    )
    return result.to_dict()


def _generated_candidate_for(
    view: _ObligationView,
    *,
    today: date | datetime | str | None,
) -> GeneratedE2ECandidateRecord:
    obligation = view.obligation
    reason_codes = _candidate_reason_codes(view, today=today)
    reason = _reason_text(reason_codes)
    journey_or_flow = _journey_or_flow(view)
    return GeneratedE2ECandidateRecord(
        candidate_id=_candidate_id(obligation.obligation_id),
        obligation_ids=(obligation.obligation_id,),
        actor=obligation.actor,
        journey_or_flow=journey_or_flow,
        risk_level=_risk_value(obligation.risk_level),
        reason=reason,
        recommended_test_type=_recommended_test_type(_enum_value(obligation.kind)),
        status="candidate",
        selected_reason=None,
        reason_codes=reason_codes,
        future_marker=_future_marker(view),
    )


def _candidate_reason_codes(
    view: _ObligationView,
    *,
    today: date | datetime | str | None,
) -> tuple[str, ...]:
    obligation = view.obligation
    codes: list[str] = []
    status = effective_coverage_status(obligation, today=today)
    if status == CoverageStatus.UNCOVERED:
        codes.append("uncovered")
    if _risk_value(obligation.risk_level) in _HIGH_RISK:
        codes.append("high_risk")
    if _has_skip_evidence(obligation):
        codes.append("skip_evidence")
    if _is_implicit_opt_out(view.declared_status):
        codes.append("implicit_opt_out")
    waiver_code = _waiver_gap_code(obligation, today=today)
    if waiver_code is not None:
        codes.append(waiver_code)
    return _dedupe(codes) or ("uncovered",)


def _exclusion_record(
    view: _ObligationView,
    *,
    today: date | datetime | str | None,
) -> dict[str, Any] | None:
    obligation = view.obligation
    status = effective_coverage_status(obligation, today=today)
    if status == CoverageStatus.COVERED_BY_E2E:
        code = "already_covered_by_e2e"
    elif status == CoverageStatus.COVERED_BY_LOWER_TEST:
        code = "delegated_to_lower_test"
    elif status == CoverageStatus.WAIVED_WITH_REASON_AND_EXPIRY:
        code = "active_waiver"
    else:
        return None
    return {
        "obligation_id": obligation.obligation_id,
        "actor": obligation.actor,
        "coverage_status": _enum_value(status),
        "reason_code": code,
        "reason": _reason_text((code,)),
    }


def _required_obligation_ids(
    views: Sequence[_ObligationView],
    *,
    today: date | datetime | str | None,
) -> tuple[str, ...]:
    ids: list[str] = []
    for view in sorted(views, key=_view_sort_key):
        if _exclusion_record(view, today=today) is None:
            ids.append(view.obligation.obligation_id)
    return tuple(ids)


def _trace_rows(
    views: Sequence[_ObligationView],
    generated: Sequence[GeneratedE2ECandidateRecord],
    selected: Sequence[SelectedE2ESuiteRecord],
    excluded: Sequence[Mapping[str, Any]],
    *,
    today: date | datetime | str | None,
) -> tuple[dict[str, Any], ...]:
    generated_by_id: dict[str, list[str]] = {}
    for candidate in generated:
        for obligation_id in candidate.obligation_ids:
            generated_by_id.setdefault(obligation_id, []).append(candidate.candidate_id)

    selected_by_id: dict[str, list[str]] = {}
    for item in selected:
        for obligation_id in item.obligation_ids:
            selected_by_id.setdefault(obligation_id, []).append(item.candidate_id)

    exclusion_by_id = {str(item.get("obligation_id")): dict(item) for item in excluded}
    rows: list[dict[str, Any]] = []
    for view in sorted(views, key=_view_sort_key):
        obligation = view.obligation
        status = effective_coverage_status(obligation, today=today)
        rows.append(
            {
                "obligation_id": obligation.obligation_id,
                "coverage_status": _enum_value(status),
                "covered_by": [_evidence_to_dict(item) for item in obligation.covered_by],
                "waiver_reason": obligation.waiver_reason,
                "waiver_expiry": _date_to_text(obligation.waiver_expiry),
                "generated_candidate_ids": sorted(generated_by_id.get(obligation.obligation_id, [])),
                "selected_candidate_ids": sorted(selected_by_id.get(obligation.obligation_id, [])),
                "excluded_reason": exclusion_by_id.get(obligation.obligation_id, {}).get("reason_code"),
                "exclusion_reason": exclusion_by_id.get(obligation.obligation_id, {}).get("reason"),
            }
        )
    return tuple(rows)


def _unselected_records(
    generated: Sequence[GeneratedE2ECandidateRecord],
    selected: Sequence[SelectedE2ESuiteRecord],
    selected_obligation_ids: set[str],
) -> tuple[dict[str, Any], ...]:
    selected_candidate_ids = {item.candidate_id for item in selected}
    records: list[dict[str, Any]] = []
    for candidate in sorted(generated, key=_candidate_sort_key):
        if candidate.candidate_id in selected_candidate_ids:
            continue
        covered_elsewhere = set(candidate.obligation_ids) <= selected_obligation_ids
        reason_code = "covered_by_selected_candidate" if covered_elsewhere else "not_selected_by_set_cover"
        record = candidate.to_dict()
        record["unselected_reason_code"] = reason_code
        record["unselected_reason"] = _reason_text((reason_code,))
        records.append(record)
    return tuple(records)


def _selection_rank(
    candidate: GeneratedE2ECandidateRecord,
    new_ids: set[str],
    risk_by_id: Mapping[str, str],
    duplicate_cost: int,
) -> tuple[Any, ...]:
    risk_score = sum(_RISK_WEIGHT.get(risk_by_id.get(obligation_id, "P3"), 1) for obligation_id in new_ids)
    highest_risk = min((_RISK_RANK.get(risk_by_id.get(obligation_id, "P3"), 99) for obligation_id in new_ids), default=99)
    return (
        -risk_score,
        -len(new_ids),
        duplicate_cost,
        float(candidate.cost),
        highest_risk,
        _RISK_RANK.get(candidate.risk_level, 99),
        candidate.actor,
        candidate.journey_or_flow,
        candidate.candidate_id,
    )


def _selected_from_candidate(
    candidate: GeneratedE2ECandidateRecord,
    newly_covered: set[str],
    order: int,
    risk_by_id: Mapping[str, str],
) -> SelectedE2ESuiteRecord:
    ordered_ids = tuple(sorted(newly_covered, key=lambda item: (_RISK_RANK.get(risk_by_id.get(item, "P3"), 99), item)))
    highest = min((risk_by_id.get(obligation_id, "P3") for obligation_id in ordered_ids), key=lambda risk: _RISK_RANK.get(risk, 99))
    return SelectedE2ESuiteRecord(
        candidate_id=candidate.candidate_id,
        obligation_ids=ordered_ids,
        actor=candidate.actor,
        journey_or_flow=candidate.journey_or_flow,
        risk_level=highest,
        reason=candidate.reason,
        recommended_test_type=candidate.recommended_test_type,
        status="selected",
        selected_reason=(
            f"set_cover_iteration={order}; covers={len(ordered_ids)}; highest_risk={highest}"
        ),
        reason_codes=candidate.reason_codes,
        future_marker=candidate.future_marker,
        selection_order=order,
    )


def _obligation_views(
    obligations: Iterable[CoverageObligation | Mapping[str, Any] | Any],
    *,
    today: date | datetime | str | None,
) -> tuple[_ObligationView, ...]:
    return tuple(_obligation_view(item, today=today) for item in obligations)


def _obligation_view(
    raw: CoverageObligation | Mapping[str, Any] | Any,
    *,
    today: date | datetime | str | None,
) -> _ObligationView:
    payload: Mapping[str, Any]
    metadata: Mapping[str, Any] = {}
    declared_status: str | None = None

    if isinstance(raw, CoverageObligation):
        payload = _canonical_obligation_mapping(raw)
        declared_status = _optional_text(payload.get("coverage_status"))
        obligation = coverage_obligation_from_mapping(payload, today=today)
    elif isinstance(raw, Mapping):
        payload = raw.get("coverage_obligation") if "coverage_obligation" in raw else raw
        if not isinstance(payload, Mapping):
            raise TypeError("coverage_obligation payload must be a mapping")
        declared_status = _optional_text(payload.get("coverage_status"))
        metadata = _mapping_or_empty(payload.get("metadata") or raw.get("metadata"))
        obligation = coverage_obligation_from_mapping(payload, today=today)
    elif hasattr(raw, "to_schema_mapping"):
        payload = raw.to_schema_mapping()
        if not isinstance(payload, Mapping):
            raise TypeError("to_schema_mapping must return a mapping")
        declared_status = _optional_text(payload.get("coverage_status"))
        metadata = _mapping_or_empty(getattr(raw, "metadata", None))
        obligation = coverage_obligation_from_mapping(payload, today=today)
    else:
        raise TypeError("obligations must be CoverageObligation objects or schema mappings")

    return _ObligationView(
        obligation=obligation,
        declared_status=declared_status,
        payload=dict(payload),
        metadata=dict(metadata),
    )


def _canonical_obligation_mapping(obligation: CoverageObligation) -> dict[str, Any]:
    return {
        "obligation_id": obligation.obligation_id,
        "source": {
            "type": _enum_value(obligation.source.type),
            "ref": obligation.source.ref,
        },
        "kind": _enum_value(obligation.kind),
        "actor": obligation.actor,
        "goal": obligation.goal,
        "preconditions": list(obligation.preconditions),
        "expected_outcomes": list(obligation.expected_outcomes),
        "side_effects": list(obligation.side_effects),
        "risk_level": _risk_value(obligation.risk_level),
        "coverage_status": _enum_value(obligation.coverage_status),
        "covered_by": [_evidence_to_dict(item) for item in obligation.covered_by],
        "waiver_reason": obligation.waiver_reason,
        "waiver_expiry": _date_to_text(obligation.waiver_expiry),
    }


def _candidate_record(raw: GeneratedE2ECandidateRecord | Mapping[str, Any]) -> GeneratedE2ECandidateRecord:
    if isinstance(raw, GeneratedE2ECandidateRecord):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("candidate must be a GeneratedE2ECandidateRecord or mapping")
    return GeneratedE2ECandidateRecord(
        candidate_id=str(raw.get("candidate_id") or raw.get("id") or ""),
        obligation_ids=raw.get("obligation_ids") or raw.get("covers") or (),
        actor=str(raw.get("actor") or "system"),
        journey_or_flow=str(raw.get("journey_or_flow") or raw.get("flow") or "unspecified"),
        risk_level=str(raw.get("risk_level") or "P3"),
        reason=str(raw.get("reason") or ""),
        recommended_test_type=str(raw.get("recommended_test_type") or "browser_e2e"),
        status=str(raw.get("status") or "candidate"),
        selected_reason=_optional_text(raw.get("selected_reason")),
        reason_codes=raw.get("reason_codes") or (),
        future_marker=_mapping_or_empty(raw.get("future_marker")),
        cost=float(raw.get("cost") or 1.0),
    )


def _selected_record(raw: SelectedE2ESuiteRecord | Mapping[str, Any]) -> SelectedE2ESuiteRecord:
    if isinstance(raw, SelectedE2ESuiteRecord):
        return raw
    candidate = _candidate_record(raw)
    return SelectedE2ESuiteRecord(
        candidate_id=candidate.candidate_id,
        obligation_ids=candidate.obligation_ids,
        actor=candidate.actor,
        journey_or_flow=candidate.journey_or_flow,
        risk_level=candidate.risk_level,
        reason=candidate.reason,
        recommended_test_type=candidate.recommended_test_type,
        status=str(raw.get("status") or "selected"),
        selected_reason=_optional_text(raw.get("selected_reason")),
        reason_codes=candidate.reason_codes,
        future_marker=candidate.future_marker,
        selection_order=int(raw.get("selection_order") or 0),
    )


def _view_sort_key(view: _ObligationView) -> tuple[Any, ...]:
    obligation = view.obligation
    return (
        _RISK_RANK.get(_risk_value(obligation.risk_level), 99),
        obligation.actor,
        _enum_value(obligation.kind),
        _journey_or_flow(view),
        obligation.obligation_id,
    )


def _candidate_sort_key(candidate: GeneratedE2ECandidateRecord) -> tuple[Any, ...]:
    return (
        _RISK_RANK.get(candidate.risk_level, 99),
        candidate.actor,
        candidate.journey_or_flow,
        candidate.candidate_id,
    )


def _candidate_fingerprint(candidate: GeneratedE2ECandidateRecord) -> tuple[str, tuple[str, ...]]:
    return (candidate.journey_or_flow, tuple(sorted(candidate.obligation_ids)))


def _candidate_id(obligation_id: str) -> str:
    return "candidate:e2e:" + _slug(obligation_id)


def _recommended_test_type(kind: str) -> str:
    return {
        "role_sequence": "browser_role_sequence",
        "action_outcome": "browser_action_outcome",
        "global_action": "browser_global_action",
        "breakpoint_coverage": "browser_breakpoint_e2e",
        "crud_flow": "browser_crud_flow",
        "connectivity": "runtime_connectivity_e2e",
        "presentation_locale": "browser_presentation_e2e",
        "aggregation_policy": "browser_aggregation_e2e",
        "runtime_capability": "runtime_capability_e2e",
    }.get(kind, "browser_e2e")


def _journey_or_flow(view: _ObligationView) -> str:
    for key in ("journey_name", "journey_or_flow", "flow", "name"):
        value = view.metadata.get(key) or view.payload.get(key)
        if _optional_text(value):
            return str(value)
    return view.obligation.goal or view.obligation.obligation_id


def _future_marker(view: _ObligationView) -> dict[str, Any]:
    return {
        "pairwise": _future_marker_item(view, ("pairwise_parameters", "pairwise_axes")),
        "t_way": _future_marker_item(view, ("t_way_parameters", "t_way_axes", "tway_parameters")),
    }


def _future_marker_item(view: _ObligationView, keys: Sequence[str]) -> dict[str, Any]:
    value = _first_present(view.payload, view.metadata, keys)
    return {
        "status": "future_todo",
        "declared": value is not None,
        "parameters": value if isinstance(value, Mapping) else {},
        "reason": "selection marker only; not counted as covered",
    }


def _first_present(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    keys: Sequence[str],
) -> Any:
    for mapping in (primary, secondary):
        for key in keys:
            if key in mapping:
                return mapping[key]
    return None


def _waiver_gap_code(
    obligation: CoverageObligation,
    *,
    today: date | datetime | str | None,
) -> str | None:
    if not obligation.waiver_reason and not obligation.waiver_expiry:
        return None
    expiry = _parse_date(obligation.waiver_expiry)
    if not obligation.waiver_reason or expiry is None:
        return "invalid_waiver"
    if expiry <= _today(today):
        return "expired_waiver"
    return None


def _has_skip_evidence(obligation: CoverageObligation) -> bool:
    return any(item.is_skip for item in obligation.covered_by)


def _is_implicit_opt_out(declared_status: str | None) -> bool:
    token = _normalize_token(declared_status)
    return token in {"", "none", "not_applicable", "opt_out", "skip", "skipped"}


def _reason_text(codes: Sequence[str]) -> str:
    labels = {
        "uncovered": "No valid E2E, lower-level delegation, or active waiver covers this obligation.",
        "high_risk": "High-risk obligation requires explicit E2E attention.",
        "skip_evidence": "Recorded evidence is skipped and cannot count as green coverage.",
        "implicit_opt_out": "Implicit opt-out is incomplete without an explicit active waiver.",
        "expired_waiver": "Waiver expiry is in the past.",
        "invalid_waiver": "Waiver is missing a reason or valid expiry.",
        "already_covered_by_e2e": "Already covered by valid E2E evidence.",
        "delegated_to_lower_test": "Covered by a valid lower-level test delegation.",
        "active_waiver": "Covered by an active waiver with reason and future expiry.",
        "covered_by_selected_candidate": "Another selected candidate already covers these obligations.",
        "not_selected_by_set_cover": "Candidate was not chosen by deterministic set cover within the selection budget.",
    }
    return " ".join(labels.get(code, code) for code in codes)


def _risk_value(value: Any) -> str:
    text = _enum_value(value).upper()
    return text if text in _RISK_RANK else "P3"


def _enum_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value or "")


def _string_tuple(value: Sequence[Any] | str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value if item is not None)


def _optional_text(value: Any) -> str | None:
    text = _clean_text(value)
    return text or None


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _slug(value: str) -> str:
    slug = []
    previous_sep = False
    for char in value.lower():
        if char.isalnum():
            slug.append(char)
            previous_sep = False
        elif not previous_sep:
            slug.append("_")
            previous_sep = True
    return "".join(slug).strip("_") or "obligation"


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


def _date_to_text(value: date | datetime | str | None) -> str | None:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed is not None else None


def _evidence_to_dict(evidence: CoverageEvidence) -> dict[str, Any]:
    return {
        "type": evidence.type,
        "ref": evidence.ref,
        "status": evidence.status,
        "skipped": evidence.skipped,
        "skip_reason": evidence.skip_reason,
    }


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


__all__ = [
    "CoverageCandidateSelectionResult",
    "GeneratedE2ECandidateRecord",
    "SelectedE2ESuiteRecord",
    "candidate_selection_payload",
    "generate_e2e_candidates",
    "select_e2e_suite",
]
