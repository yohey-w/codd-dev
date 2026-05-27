"""Extract coverage obligations from existing CoDD declarations."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from codd.config import load_project_config
from codd.dag import DAG, Node
from codd.dag.builder import build_dag

try:
    from codd.coverage_obligations import coverage_obligation_from_mapping as _schema_obligation_from_mapping
except ImportError:  # pragma: no cover - parallel slice may not be present during isolated development
    _schema_obligation_from_mapping = None


ROLE_SEQUENCE = "role_sequence"
ACTION_OUTCOME = "action_outcome"
GLOBAL_ACTION = "global_action"
CRUD_FLOW = "crud_flow"
PRESENTATION_LOCALE = "presentation_locale"
AGGREGATION_POLICY = "aggregation_policy"

COVERED_BY_E2E = "covered_by_e2e"
COVERED_BY_LOWER_TEST = "covered_by_lower_test"
WAIVED_WITH_REASON_AND_EXPIRY = "waived_with_reason_and_expiry"
UNCOVERED = "uncovered"
SUPPORTED_STATUSES = {
    COVERED_BY_E2E,
    COVERED_BY_LOWER_TEST,
    WAIVED_WITH_REASON_AND_EXPIRY,
    UNCOVERED,
}

PRESENTATION_ATTRIBUTE_KEYS = ("display_fields", "presentation_specs")
AGGREGATION_ATTRIBUTE_KEYS = ("aggregation_policies",)
ACTOR_KEYS = {
    "actor",
    "actors",
    "role",
    "roles",
    "stakeholder",
    "stakeholders",
    "stakeholder_roles",
}


@dataclass(frozen=True)
class EvidenceCandidate:
    """A declared verification artifact that may cover an obligation."""

    type: str
    ref: str
    source: str
    kind: str | None = None
    journey_name: str | None = None
    target: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(asdict(self))


@dataclass(frozen=True)
class CoverageObligation:
    """Normalized coverage obligation extracted from existing declarations."""

    obligation_id: str
    kind: str
    source: dict[str, str]
    actor: str = "system"
    goal: str = ""
    preconditions: tuple[str, ...] = ()
    expected_outcomes: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    risk_level: str = "P2"
    coverage_status: str = UNCOVERED
    covered_by: tuple[dict[str, str], ...] = ()
    waiver_reason: str | None = None
    waiver_expiry: str | None = None
    sequence_steps: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_empty(asdict(self))

    def to_schema_mapping(self) -> dict[str, Any]:
        """Return the canonical schema shape consumed by coverage_obligations."""

        return {
            "obligation_id": self.obligation_id,
            "source": self.source,
            "kind": self.kind,
            "actor": self.actor,
            "goal": self.goal,
            "preconditions": list(self.preconditions),
            "expected_outcomes": list(self.expected_outcomes),
            "side_effects": list(self.side_effects),
            "risk_level": self.risk_level,
            "coverage_status": self.coverage_status,
            "covered_by": list(self.covered_by),
            "waiver_reason": self.waiver_reason,
            "waiver_expiry": self.waiver_expiry,
        }

    def to_schema_obligation(self) -> Any:
        """Build a canonical CoverageObligation when that schema module is available."""

        if _schema_obligation_from_mapping is None:
            return self.to_schema_mapping()
        return _schema_obligation_from_mapping(self.to_schema_mapping())


@dataclass
class CoverageObligationExtractionResult:
    """Extraction output for obligations, evidence candidates, and unsupported gaps."""

    obligations: list[CoverageObligation] = field(default_factory=list)
    evidence_candidates: list[EvidenceCandidate] = field(default_factory=list)
    unsupported_items: list[dict[str, Any]] = field(default_factory=list)

    def obligations_by_kind(self, kind: str) -> list[CoverageObligation]:
        return [obligation for obligation in self.obligations if obligation.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligations": [obligation.to_dict() for obligation in self.obligations],
            "evidence_candidates": [candidate.to_dict() for candidate in self.evidence_candidates],
            "unsupported_items": [item for item in self.unsupported_items if item],
        }


def extract_coverage_obligations(
    project_root: str | Path,
    *,
    settings: dict[str, Any] | None = None,
    project_config: dict[str, Any] | None = None,
    dag: DAG | None = None,
) -> CoverageObligationExtractionResult:
    """Build or use a DAG, then extract obligation declarations.

    The extractor is intentionally conservative: declared verification tests are
    returned as evidence candidates and are not treated as proof of coverage by
    themselves.
    """

    root = Path(project_root)
    config = project_config if project_config is not None else _load_config_or_empty(root)
    target_dag = dag if dag is not None else build_dag(root, settings)
    return extract_coverage_obligations_from_dag(target_dag, project_config=config)


def extract_coverage_obligations_from_dag(
    dag: DAG,
    *,
    project_config: dict[str, Any] | None = None,
) -> CoverageObligationExtractionResult:
    """Extract obligation records from an already-built DAG and config mapping."""

    config = project_config or {}
    evidence_candidates = _extract_evidence_candidates(dag)
    obligations: list[CoverageObligation] = []
    unsupported_items: list[dict[str, Any]] = []

    obligations.extend(_extract_role_sequence_obligations(dag, evidence_candidates))
    obligations.extend(_extract_runtime_target_obligations(config, evidence_candidates))
    obligations.extend(_extract_presentation_obligations(dag, unsupported_items))
    obligations.extend(_extract_aggregation_obligations(dag, unsupported_items))
    obligations.extend(_missing_role_sequence_obligations(dag, config, obligations))

    _add_missing_c7_report_items(dag, obligations, unsupported_items)
    return CoverageObligationExtractionResult(
        obligations=_dedupe_obligations(obligations),
        evidence_candidates=evidence_candidates,
        unsupported_items=unsupported_items,
    )


def _extract_role_sequence_obligations(
    dag: DAG,
    evidence_candidates: list[EvidenceCandidate],
) -> list[CoverageObligation]:
    obligations: list[CoverageObligation] = []
    for design_doc in _design_doc_nodes(dag):
        for index, journey in enumerate(_mapping_list(design_doc.attributes.get("user_journeys"))):
            name = _text(journey.get("name")) or f"journey_{index + 1}"
            actors = _actors_from_mapping(journey) or ("system",)
            evidence = _evidence_for_journey(name, evidence_candidates)
            metadata = {
                "journey_name": name,
                "criticality": journey.get("criticality"),
                "required_capabilities": _string_tuple(journey.get("required_capabilities")),
                "expected_outcome_refs": _string_tuple(journey.get("expected_outcome_refs")),
                "evidence_candidates": [candidate.to_dict() for candidate in evidence],
            }
            for actor in actors:
                obligations.append(
                    CoverageObligation(
                        obligation_id=_obligation_id(ROLE_SEQUENCE, actor, name),
                        kind=ROLE_SEQUENCE,
                        source={"type": "design_doc", "ref": f"{design_doc.id}#user_journeys[{index}]"},
                        actor=actor,
                        goal=_goal_from_mapping(journey, fallback=name),
                        preconditions=_string_tuple(journey.get("preconditions")),
                        expected_outcomes=_expected_outcomes_from_journey(journey),
                        side_effects=_string_tuple(journey.get("side_effects")),
                        risk_level=_risk_from_value(journey.get("risk_level", journey.get("criticality"))),
                        coverage_status=_coverage_status(journey),
                        sequence_steps=_sequence_steps(journey.get("steps")),
                        metadata=metadata,
                    )
                )
    return obligations


def _extract_runtime_target_obligations(
    project_config: dict[str, Any],
    evidence_candidates: list[EvidenceCandidate],
) -> list[CoverageObligation]:
    runtime = project_config.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}

    obligations: list[CoverageObligation] = []
    obligations.extend(
        _extract_action_target_obligations(
            runtime.get("global_action_targets"),
            kind=GLOBAL_ACTION,
            source_key="runtime.global_action_targets",
            evidence_candidates=evidence_candidates,
        )
    )
    obligations.extend(
        _extract_action_target_obligations(
            runtime.get("action_outcome_targets"),
            kind=ACTION_OUTCOME,
            source_key="runtime.action_outcome_targets",
            evidence_candidates=evidence_candidates,
        )
    )
    obligations.extend(_extract_crud_flow_obligations(runtime.get("crud_flow_targets"), evidence_candidates))
    return obligations


def _extract_action_target_obligations(
    raw_targets: Any,
    *,
    kind: str,
    source_key: str,
    evidence_candidates: list[EvidenceCandidate],
) -> list[CoverageObligation]:
    obligations: list[CoverageObligation] = []
    for target_index, target in enumerate(_mapping_list(raw_targets)):
        target_name = _text(target.get("name")) or f"{kind}_{target_index + 1}"
        actions = _mapping_list(target.get("actions", target.get("action"))) or [target]
        for action_index, action in enumerate(actions):
            action_name = _text(action.get("id", action.get("name"))) or target_name
            actors = _actors_from_mapping(action) or _actors_from_mapping(target) or ("system",)
            expected = _outcomes_from_action_target(action, target)
            evidence = _evidence_for_ref(target.get("command"), evidence_candidates)
            source_ref = f"codd.yaml#{source_key}[{target_index}]"
            if len(actions) > 1:
                source_ref = f"{source_ref}.actions[{action_index}]"
            for actor in actors:
                obligations.append(
                    CoverageObligation(
                        obligation_id=_obligation_id(kind, actor, action_name),
                        kind=kind,
                        source={"type": "runtime", "ref": source_ref},
                        actor=actor,
                        goal=_action_goal(kind, target_name, action),
                        preconditions=_string_tuple(action.get("preconditions", target.get("preconditions"))),
                        expected_outcomes=expected,
                        risk_level=_risk_from_value(target.get("risk_level", target.get("criticality"))),
                        coverage_status=_coverage_status(target),
                        covered_by=_declared_covered_by(target),
                        metadata={
                            "target_name": target_name,
                            "action_id": action_name,
                            "verb": action.get("verb"),
                            "target": action.get("target"),
                            "trigger": action.get("trigger"),
                            "command": target.get("command"),
                            "invoke": _connection_summary(target.get("invoke")),
                            "observe": _connection_summary(target.get("observe")),
                            "evidence_candidates": [candidate.to_dict() for candidate in evidence],
                        },
                    )
                )
    return obligations


def _extract_crud_flow_obligations(
    raw_targets: Any,
    evidence_candidates: list[EvidenceCandidate],
) -> list[CoverageObligation]:
    obligations: list[CoverageObligation] = []
    for index, target in enumerate(_mapping_list(raw_targets)):
        name = _text(target.get("name")) or f"crud_flow_{index + 1}"
        actors = _actors_from_mapping(target) or ("system",)
        evidence = _evidence_for_ref(target.get("command"), evidence_candidates)
        for actor in actors:
            obligations.append(
                CoverageObligation(
                    obligation_id=_obligation_id(CRUD_FLOW, actor, name),
                    kind=CRUD_FLOW,
                    source={"type": "runtime", "ref": f"codd.yaml#runtime.crud_flow_targets[{index}]"},
                    actor=actor,
                    goal=_text(target.get("goal")) or name,
                    preconditions=_string_tuple(target.get("preconditions")),
                    expected_outcomes=_crud_expected_outcomes(target),
                    risk_level=_risk_from_value(target.get("risk_level", target.get("criticality"))),
                    coverage_status=_coverage_status(target),
                    covered_by=_declared_covered_by(target),
                    metadata={
                        "target_name": name,
                        "command": target.get("command"),
                        "create": _connection_summary(target.get("create")),
                        "reflect": _connection_summary(target.get("reflect")),
                        "evidence_candidates": [candidate.to_dict() for candidate in evidence],
                    },
                )
            )
    return obligations


def _extract_presentation_obligations(
    dag: DAG,
    unsupported_items: list[dict[str, Any]],
) -> list[CoverageObligation]:
    obligations: list[CoverageObligation] = []
    for design_doc in _design_doc_nodes(dag):
        for key in PRESENTATION_ATTRIBUTE_KEYS:
            raw_entries = design_doc.attributes.get(key)
            if raw_entries is None:
                continue
            entries = _mapping_list(raw_entries)
            if not entries:
                unsupported_items.append(_unsupported_item(PRESENTATION_LOCALE, design_doc.id, key, raw_entries))
                continue
            for index, entry in enumerate(entries):
                name = _text(entry.get("name", entry.get("field", entry.get("id")))) or f"{key}_{index + 1}"
                for actor in _actors_from_mapping(entry) or ("system",):
                    obligations.append(
                        CoverageObligation(
                            obligation_id=_obligation_id(PRESENTATION_LOCALE, actor, name),
                            kind=PRESENTATION_LOCALE,
                            source={"type": "design_doc", "ref": f"{design_doc.id}#{key}[{index}]"},
                            actor=actor,
                            goal=_presentation_goal(entry, name),
                            preconditions=_string_tuple(entry.get("preconditions")),
                            expected_outcomes=_presentation_expected_outcomes(entry),
                            risk_level=_risk_from_value(entry.get("risk_level", entry.get("criticality"))),
                            coverage_status=_coverage_status(entry),
                            metadata={"attribute_key": key, "raw": entry},
                        )
                    )
    return obligations


def _extract_aggregation_obligations(
    dag: DAG,
    unsupported_items: list[dict[str, Any]],
) -> list[CoverageObligation]:
    obligations: list[CoverageObligation] = []
    for design_doc in _design_doc_nodes(dag):
        for key in AGGREGATION_ATTRIBUTE_KEYS:
            raw_entries = design_doc.attributes.get(key)
            if raw_entries is None:
                continue
            entries = _mapping_list(raw_entries)
            if not entries:
                unsupported_items.append(_unsupported_item(AGGREGATION_POLICY, design_doc.id, key, raw_entries))
                continue
            for index, entry in enumerate(entries):
                name = _text(entry.get("name", entry.get("metric", entry.get("id")))) or f"{key}_{index + 1}"
                for actor in _actors_from_mapping(entry) or ("system",):
                    obligations.append(
                        CoverageObligation(
                            obligation_id=_obligation_id(AGGREGATION_POLICY, actor, name),
                            kind=AGGREGATION_POLICY,
                            source={"type": "design_doc", "ref": f"{design_doc.id}#{key}[{index}]"},
                            actor=actor,
                            goal=_aggregation_goal(entry, name),
                            preconditions=_string_tuple(entry.get("preconditions")),
                            expected_outcomes=_aggregation_expected_outcomes(entry),
                            risk_level=_risk_from_value(entry.get("risk_level", entry.get("criticality"))),
                            coverage_status=_coverage_status(entry),
                            metadata={"attribute_key": key, "raw": entry},
                        )
                    )
    return obligations


def _missing_role_sequence_obligations(
    dag: DAG,
    project_config: dict[str, Any],
    obligations: list[CoverageObligation],
) -> list[CoverageObligation]:
    declared = {
        _normalize_actor(obligation.actor)
        for obligation in obligations
        if obligation.kind == ROLE_SEQUENCE and obligation.actor != "system"
    }
    missing: list[CoverageObligation] = []
    for actor, source_type, source_ref in _actor_sources(dag, project_config):
        if _normalize_actor(actor) in declared:
            continue
        missing.append(
            CoverageObligation(
                obligation_id=_obligation_id(ROLE_SEQUENCE, actor, "missing_journey"),
                kind=ROLE_SEQUENCE,
                source={"type": source_type, "ref": source_ref},
                actor=actor,
                goal=f"Declare and verify a meaningful workflow for {actor}.",
                risk_level="P1",
                coverage_status=UNCOVERED,
                metadata={
                    "severity": "amber",
                    "inference": "actor_without_role_sequence",
                    "inferred_source_type": "inferred",
                    "remediation": "Declare user_journeys or add an explicit waiver with reason and expiry.",
                },
            )
        )
    return missing


def _extract_evidence_candidates(dag: DAG) -> list[EvidenceCandidate]:
    candidates: list[EvidenceCandidate] = []
    for node in sorted(dag.nodes.values(), key=lambda item: item.id):
        if node.kind == "verification_test":
            candidate = _verification_test_candidate(node)
            if candidate is not None:
                candidates.append(candidate)
        elif node.kind == "test_file" and _looks_like_e2e_path(node.path or node.id):
            candidates.append(
                EvidenceCandidate(
                    type="verification_test",
                    ref=node.path or node.id,
                    source=node.id,
                    kind="e2e",
                    target=node.path or node.id,
                    attributes={"node_kind": node.kind},
                )
            )
    return _dedupe_evidence_candidates(candidates)


def _verification_test_candidate(node: Node) -> EvidenceCandidate | None:
    attributes = node.attributes or {}
    kind = _text(attributes.get("kind")) or "verification_test"
    outcome = attributes.get("expected_outcome")
    source = outcome.get("source") if isinstance(outcome, dict) else None
    ref = _text(source) or node.path or node.id
    journey_name = _text(attributes.get("journey_name"))
    if journey_name is None and isinstance(outcome, dict):
        journey_name = _text(outcome.get("journey_name"))
    return EvidenceCandidate(
        type="verification_test",
        ref=ref,
        source=node.id,
        kind=kind,
        journey_name=journey_name,
        target=_text(attributes.get("target")),
        attributes={
            "node_kind": node.kind,
            "verified_by": _string_tuple(attributes.get("verified_by")),
            "axis_matrix": attributes.get("axis_matrix") if isinstance(attributes.get("axis_matrix"), list) else (),
        },
    )


def _add_missing_c7_report_items(
    dag: DAG,
    obligations: list[CoverageObligation],
    unsupported_items: list[dict[str, Any]],
) -> None:
    if not _design_doc_nodes(dag):
        return
    if not any(obligation.kind == PRESENTATION_LOCALE for obligation in obligations):
        unsupported_items.append(
            {
                "kind": PRESENTATION_LOCALE,
                "source": {"type": "design_doc", "ref": "(all)"},
                "reason": "No display_fields or presentation_specs declarations were present in DAG attributes.",
                "status": "unsupported",
            }
        )
    if not any(obligation.kind == AGGREGATION_POLICY for obligation in obligations):
        unsupported_items.append(
            {
                "kind": AGGREGATION_POLICY,
                "source": {"type": "design_doc", "ref": "(all)"},
                "reason": "No aggregation_policies declarations were present in DAG attributes.",
                "status": "unsupported",
            }
        )


def _design_doc_nodes(dag: DAG) -> list[Node]:
    return [node for node in sorted(dag.nodes.values(), key=lambda item: item.id) if node.kind == "design_doc"]


def _actor_sources(dag: DAG, project_config: dict[str, Any]) -> list[tuple[str, str, str]]:
    actors: list[tuple[str, str, str]] = []
    for node in sorted(dag.nodes.values(), key=lambda item: item.id):
        source_type = _actor_source_type_for_node(node)
        actors.extend((actor, source_type, node.id) for actor in _actors_from_mapping(node.attributes))
        details = node.attributes.get("details")
        if isinstance(details, dict):
            actors.extend((actor, source_type, f"{node.id}#details") for actor in _actors_from_mapping(details))
            if _is_actor_dimension(details):
                actors.extend(
                    (actor, source_type, f"{node.id}#details") for actor in _actors_from_actor_dimension(details)
                )
        if _is_actor_dimension(node.attributes):
            actors.extend((actor, source_type, node.id) for actor in _actors_from_actor_dimension(node.attributes))

    runtime = project_config.get("runtime") if isinstance(project_config.get("runtime"), dict) else {}
    for key in ("global_action_targets", "action_outcome_targets", "crud_flow_targets"):
        for index, target in enumerate(_mapping_list(runtime.get(key))):
            actors.extend((actor, "runtime", f"codd.yaml#runtime.{key}[{index}]") for actor in _actors_from_mapping(target))
            for action_index, action in enumerate(_mapping_list(target.get("actions", target.get("action")))):
                actors.extend(
                    (actor, "runtime", f"codd.yaml#runtime.{key}[{index}].actions[{action_index}]")
                    for actor in _actors_from_mapping(action)
                )

    deduped: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for actor, source_type, source_ref in actors:
        key = _normalize_actor(actor)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((actor, source_type, source_ref))
    return deduped


def _actor_source_type_for_node(node: Node) -> str:
    node_kind = str(node.kind or "").strip().lower().replace("-", "_")
    if node_kind == "design_doc":
        return "design_doc"
    if "requirement" in node_kind:
        return "requirement"
    if "lexicon" in node_kind:
        return "lexicon"
    return "static"


def _actors_from_actor_dimension(mapping: dict[str, Any]) -> tuple[str, ...]:
    actors: list[str] = []
    for key in ("value", "values", "item", "items", "candidate", "candidates", "name", "names"):
        if key in mapping:
            actors.extend(_actors_from_value(mapping[key]))
    return _string_tuple(actors)


def _actors_from_mapping(mapping: dict[str, Any]) -> tuple[str, ...]:
    actors: list[str] = []
    for key, value in mapping.items():
        if str(key).strip().lower() in ACTOR_KEYS:
            actors.extend(_actors_from_value(value))
    return _string_tuple(actors)


def _actors_from_value(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [actor for item in re.split(r"[,;\n]+", value) if (actor := _clean_actor_name(item))]
    if isinstance(value, dict):
        for key in ("name", "id", "label", "role", "actor", "stakeholder"):
            if key in value:
                return _actors_from_value(value[key])
        return []
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, str)):
        actors: list[str] = []
        for item in value:
            actors.extend(_actors_from_value(item))
        return actors
    actor = _clean_actor_name(str(value))
    return [actor] if actor else []


def _clean_actor_name(value: str) -> str | None:
    text = value.strip().strip("'\"`")
    if not text or len(text) > 80:
        return None
    if text.lower() in {
        "actor",
        "actors",
        "role",
        "roles",
        "stakeholder",
        "stakeholders",
        "covered",
        "implicit",
        "gap",
    }:
        return None
    return text


def _is_actor_dimension(attributes: dict[str, Any]) -> bool:
    for key in ("dimension", "axis", "axis_type"):
        value = attributes.get(key)
        if isinstance(value, str) and any(token in value.lower() for token in ("actor", "stakeholder", "role")):
            return True
    return False


def _goal_from_mapping(mapping: dict[str, Any], *, fallback: str) -> str:
    for key in ("goal", "purpose", "intent", "description", "summary", "name"):
        value = _text(mapping.get(key))
        if value:
            return value
    return fallback


def _action_goal(kind: str, target_name: str, action: dict[str, Any]) -> str:
    goal = _goal_from_mapping(action, fallback=target_name)
    verb = _text(action.get("verb"))
    target = _text(action.get("target"))
    if goal != target_name or not verb:
        return goal
    return " ".join(part for part in (verb, target or target_name) if part)


def _expected_outcomes_from_journey(journey: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    values.extend(_string_tuple(journey.get("expected_outcomes", journey.get("expected_outcome"))))
    values.extend(_string_tuple(journey.get("expected_outcome_refs")))
    if not values:
        values.extend(step for step in _sequence_steps(journey.get("steps")) if step.lower().startswith(("expect", "assert")))
    return _string_tuple(values)


def _outcomes_from_action_target(action: dict[str, Any], target: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    raw_outcomes = action.get("outcomes", action.get("outcome", target.get("outcomes", target.get("outcome"))))
    values.extend(_outcome_names(raw_outcomes))
    for key in ("expect_text", "forbid_text"):
        value = _text(target.get(key))
        if value:
            values.append(f"{key}: {value}")
    return _string_tuple(values)


def _crud_expected_outcomes(target: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    expect_text = _text(target.get("expect_text"))
    if expect_text:
        values.append(f"reflects text: {expect_text}")
    reflect = target.get("reflect")
    if isinstance(reflect, dict):
        reflect_text = _text(reflect.get("expect_text"))
        if reflect_text and reflect_text not in values:
            values.append(f"reflects text: {reflect_text}")
        url = _text(reflect.get("url"))
        if url:
            values.append(f"reflect endpoint: {url}")
    return _string_tuple(values or (target.get("name"),))


def _presentation_goal(entry: dict[str, Any], name: str) -> str:
    field = _text(entry.get("field", entry.get("value", entry.get("name")))) or name
    return _text(entry.get("goal")) or f"Present {field} with the declared locale, format, and timezone."


def _presentation_expected_outcomes(entry: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("locale", "timezone", "format", "expected_format", "example", "expected"):
        value = _text(entry.get(key))
        if value:
            values.append(f"{key}: {value}")
    return _string_tuple(values)


def _aggregation_goal(entry: dict[str, Any], name: str) -> str:
    subject = _text(entry.get("subject", entry.get("metric", entry.get("field")))) or name
    return _text(entry.get("goal")) or f"Aggregate {subject} according to the declared policy."


def _aggregation_expected_outcomes(entry: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("policy", "function", "source_count", "cardinality", "expected", "example"):
        value = _text(entry.get(key))
        if value:
            values.append(f"{key}: {value}")
    return _string_tuple(values)


def _outcome_names(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        values: list[str] = []
        for item in raw:
            values.extend(_outcome_names(item))
        return values
    if isinstance(raw, dict):
        name = raw.get("name") or raw.get("id") or raw.get("type")
        if name:
            return [str(name)]
        return [str(key) for key, value in raw.items() if value not in (False, None, "", "false", "skip", "skipped")]
    return [str(raw)]


def _sequence_steps(raw: Any) -> tuple[str, ...]:
    steps: list[str] = []
    for step in _as_list(raw):
        if isinstance(step, str):
            steps.append(step)
            continue
        if not isinstance(step, dict):
            continue
        action = _text(step.get("action", step.get("verb"))) or "step"
        target = _text(step.get("target", step.get("url", step.get("path", step.get("value")))))
        steps.append(": ".join(part for part in (action, target) if part))
    return _string_tuple(steps)


def _coverage_status(mapping: dict[str, Any]) -> str:
    status = _text(mapping.get("coverage_status", mapping.get("status")))
    return status if status in SUPPORTED_STATUSES else UNCOVERED


def _declared_covered_by(mapping: dict[str, Any]) -> tuple[dict[str, str], ...]:
    entries: list[dict[str, str]] = []
    for item in _as_list(mapping.get("covered_by")):
        if isinstance(item, str):
            entries.append({"type": "manual", "ref": item})
        elif isinstance(item, dict):
            ref = _text(item.get("ref", item.get("path", item.get("source"))))
            if ref:
                entries.append({"type": _text(item.get("type")) or "manual", "ref": ref})
    return tuple(entries)


def _evidence_for_journey(
    journey_name: str,
    evidence_candidates: list[EvidenceCandidate],
) -> list[EvidenceCandidate]:
    normalized = _slug(journey_name)
    return [
        candidate
        for candidate in evidence_candidates
        if candidate.journey_name == journey_name or normalized in _slug(candidate.ref) or normalized in _slug(candidate.source)
    ]


def _evidence_for_ref(raw_ref: Any, evidence_candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
    ref = _text(raw_ref)
    if not ref:
        return []
    return [candidate for candidate in evidence_candidates if candidate.ref in ref or ref in candidate.ref]


def _connection_summary(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    result = {
        key: raw.get(key)
        for key in ("name", "method", "url", "expected_status", "expect_text", "forbid_text")
        if raw.get(key) not in (None, "")
    }
    return result or None


def _mapping_list(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _string_tuple(value: Any) -> tuple[str, ...]:
    result: list[str] = []
    for item in _as_list(value):
        if item in (None, ""):
            continue
        if isinstance(item, dict):
            result.append(", ".join(f"{key}={raw_value}" for key, raw_value in item.items() if raw_value not in (None, "")))
        else:
            result.append(str(item))
    deduped: list[str] = []
    seen: set[str] = set()
    for item in result:
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return tuple(deduped)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _risk_from_value(value: Any) -> str:
    text = str(value or "").strip().lower()
    return {
        "p0": "P0",
        "critical": "P0",
        "blocker": "P0",
        "high": "P1",
        "p1": "P1",
        "medium": "P2",
        "p2": "P2",
        "low": "P3",
        "p3": "P3",
    }.get(text, "P2")


def _obligation_id(kind: str, actor: str, name: str) -> str:
    return f"obl:{kind}:{_slug(actor)}:{_slug(name)}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    return slug or "item"


def _normalize_actor(actor: str) -> str:
    return _slug(actor)


def _looks_like_e2e_path(path: str) -> bool:
    lowered = path.lower().replace("\\", "/")
    return "/e2e/" in lowered or lowered.startswith("e2e/") or ".e2e." in lowered


def _unsupported_item(kind: str, design_doc_id: str, key: str, raw_value: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "source": {"type": "design_doc", "ref": f"{design_doc_id}#{key}"},
        "reason": f"{key} is present but is not a list of mappings and cannot be extracted.",
        "status": "unsupported",
        "raw_type": type(raw_value).__name__,
    }


def _dedupe_obligations(obligations: list[CoverageObligation]) -> list[CoverageObligation]:
    deduped: list[CoverageObligation] = []
    seen: set[str] = set()
    for obligation in obligations:
        if obligation.obligation_id in seen:
            continue
        seen.add(obligation.obligation_id)
        deduped.append(obligation)
    return deduped


def _dedupe_evidence_candidates(candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
    deduped: list[EvidenceCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in candidates:
        key = (candidate.type, candidate.ref, candidate.source)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _drop_empty(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", (), [], {}) and not (key == "metadata" and item == {})
    }


def _load_config_or_empty(project_root: Path) -> dict[str, Any]:
    try:
        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return {}
