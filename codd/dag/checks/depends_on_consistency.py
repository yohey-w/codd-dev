"""Check value consistency across ``depends_on`` DAG edges."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from codd.dag.checks import register_dag_check


@dataclass(frozen=True)
class ConsistencyViolation:
    from_node: str
    to_node: str
    edge_kind: str
    value_type: str
    from_value: str
    to_value: str


@dataclass
class DependsOnConsistencyResult:
    check_name: str = "depends_on_consistency"
    severity: str = "red"
    violations: list[ConsistencyViolation] = field(default_factory=list)
    passed: bool = True
    skipped: bool = False
    warnings: list[str] = field(default_factory=list)


@register_dag_check("depends_on_consistency")
class DependsOnConsistencyCheck:
    """Consume propagation output and compare values on ``depends_on`` edges."""

    def __init__(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.dag = dag
        self.project_root = Path(project_root).resolve() if project_root else None
        self.settings = settings or {}

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> DependsOnConsistencyResult:
        dag = dag if dag is not None else self.dag
        root = Path(project_root).resolve() if project_root is not None else self.project_root
        active_settings = settings if settings is not None else self.settings
        if dag is None or root is None:
            raise ValueError("dag and project_root are required")

        output_path = _find_propagation_output(root, active_settings)
        if output_path is None:
            return DependsOnConsistencyResult(
                passed=True,
                skipped=True,
                warnings=["WARN: propagation output not found; depends_on_consistency skipped"],
            )

        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return DependsOnConsistencyResult(
                passed=False,
                warnings=[f"WARN: invalid propagation output {output_path}: {exc}"],
            )

        edge_lookup = _depends_on_edge_lookup(dag)
        violations = _dedupe_violations(
            [
                *_direct_violations(payload, edge_lookup),
                *_value_table_violations(payload, dag),
            ]
        )
        return DependsOnConsistencyResult(
            violations=violations,
            passed=len(violations) == 0,
        )


def _find_propagation_output(project_root: Path, settings: dict[str, Any] | None) -> Path | None:
    configured = _configured_output_path(settings or {})
    candidates = []
    if configured:
        candidates.append(configured)
    candidates.extend(
        [
            ".codd/propagation_results.json",
            "codd/propagation_results.json",
            ".codd/propagate_results.json",
            "codd/propagate_results.json",
            ".codd/propagate_state.json",
            "codd/propagate_state.json",
        ]
    )

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            path = project_root / path
        if path.is_file():
            return path
    return None


def _configured_output_path(settings: dict[str, Any]) -> str | None:
    for mapping in (settings, settings.get("dag", {})):
        if not isinstance(mapping, dict):
            continue
        for key in ("propagation_output", "propagation_output_path"):
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _depends_on_edge_lookup(dag: Any) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for edge in getattr(dag, "edges", []):
        if getattr(edge, "kind", None) != "depends_on":
            continue
        from_id = str(getattr(edge, "from_id"))
        to_id = str(getattr(edge, "to_id"))
        lookup[(from_id, to_id)] = "depends_on"
        lookup[(to_id, from_id)] = "depends_on"
    return lookup


def _direct_violations(
    payload: Any,
    edge_lookup: dict[tuple[str, str], str],
) -> list[ConsistencyViolation]:
    violations: list[ConsistencyViolation] = []
    for record in _iter_records(payload):
        from_node = _string_field(record, "from_node", "source_node", "source", "from", "node_a")
        to_node = _string_field(record, "to_node", "target_node", "target", "to", "node_b")
        if not from_node or not to_node:
            continue
        edge_kind = _string_field(record, "edge_kind", "edge", "kind") or edge_lookup.get((from_node, to_node))
        if edge_kind != "depends_on" and (from_node, to_node) not in edge_lookup:
            continue

        children = record.get("values") or record.get("comparisons") or record.get("checks")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    violations.extend(_comparison_violation({**record, **child}, from_node, to_node))
            continue
        violations.extend(_comparison_violation(record, from_node, to_node))
    return violations


def _comparison_violation(record: dict[str, Any], from_node: str, to_node: str) -> list[ConsistencyViolation]:
    value_type = _normalize_value_type(_string_field(record, "value_type", "type", "category", "kind"))
    from_value = _string_field(record, "from_value", "source_value", "expected", "left_value", "value_a")
    to_value = _string_field(record, "to_value", "target_value", "actual", "right_value", "value_b")
    if not value_type or from_value is None or to_value is None or from_value == to_value:
        return []
    return [
        ConsistencyViolation(
            from_node=from_node,
            to_node=to_node,
            edge_kind="depends_on",
            value_type=value_type,
            from_value=from_value,
            to_value=to_value,
        )
    ]


def _value_table_violations(payload: Any, dag: Any) -> list[ConsistencyViolation]:
    values_by_node = _extract_values_by_node(payload)
    if not values_by_node:
        return []

    violations: list[ConsistencyViolation] = []
    for edge in getattr(dag, "edges", []):
        if getattr(edge, "kind", None) != "depends_on":
            continue
        from_node = str(getattr(edge, "from_id"))
        to_node = str(getattr(edge, "to_id"))
        from_values = values_by_node.get(from_node, {})
        to_values = values_by_node.get(to_node, {})
        for key in sorted(set(from_values) & set(to_values)):
            from_value = from_values[key]
            to_value = to_values[key]
            if from_value == to_value:
                continue
            violations.append(
                ConsistencyViolation(
                    from_node=from_node,
                    to_node=to_node,
                    edge_kind="depends_on",
                    value_type=key[0],
                    from_value=from_value,
                    to_value=to_value,
                )
            )
    return violations


def _extract_values_by_node(payload: Any) -> dict[str, dict[tuple[str, str], str]]:
    values: dict[str, dict[tuple[str, str], str]] = {}
    for record in _iter_value_records(payload):
        node_id = _string_field(record, "node_id", "node", "source_node", "path")
        value_type = _normalize_value_type(_string_field(record, "value_type", "type", "category", "kind"))
        name = _string_field(record, "name", "key", "identifier", "field", "symbol") or "value"
        value = _string_field(record, "value", "literal", "actual", "expected")
        if not node_id or not value_type or value is None:
            continue
        values.setdefault(node_id, {})[(value_type, name)] = value
    return values


def _iter_records(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield from _iter_records(item)
        return
    if not isinstance(payload, dict):
        return

    if _has_any(payload, "from_node", "source_node", "source", "from", "node_a"):
        yield payload
    for key in ("violations", "comparisons", "consistency_checks", "propagations", "results", "checks"):
        child = payload.get(key)
        if isinstance(child, list):
            for item in child:
                if isinstance(item, dict):
                    yield from _iter_records(item)


def _iter_value_records(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_value_records(item)
        return
    if not isinstance(payload, dict):
        return

    values = (
        payload.get("values")
        or payload.get("node_values")
        or payload.get("values_by_node")
        or payload.get("extracted_values")
    )
    if isinstance(values, list):
        for item in values:
            if isinstance(item, dict):
                yield item
    elif isinstance(values, dict):
        yield from _flatten_value_mapping(values)

    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict):
                node_id = _string_field(node, "id", "node_id", "path")
                node_values = node.get("values")
                if node_id and isinstance(node_values, list):
                    for item in node_values:
                        if isinstance(item, dict):
                            yield {"node_id": node_id, **item}


def _flatten_value_mapping(values: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for node_id, node_values in values.items():
        if isinstance(node_values, list):
            for item in node_values:
                if isinstance(item, dict):
                    yield {"node_id": str(node_id), **item}
        elif isinstance(node_values, dict):
            for value_type, entries in node_values.items():
                if isinstance(entries, dict):
                    for name, value in entries.items():
                        yield {"node_id": str(node_id), "value_type": value_type, "name": name, "value": value}


def _dedupe_violations(violations: list[ConsistencyViolation]) -> list[ConsistencyViolation]:
    seen: set[ConsistencyViolation] = set()
    deduped: list[ConsistencyViolation] = []
    for violation in violations:
        if violation in seen:
            continue
        seen.add(violation)
        deduped.append(violation)
    return deduped


def _string_field(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if value is not None:
            return str(value)
    return None


def _normalize_value_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return {
        "uri": "url",
        "route": "url",
        "endpoint": "url",
        "schema": "type",
        "const": "constant",
    }.get(normalized, normalized)


def _has_any(record: dict[str, Any], *keys: str) -> bool:
    return any(key in record for key in keys)
