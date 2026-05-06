"""Pick a primary violation from graph order and severity data."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from math import inf
from typing import Any


_SEVERITY_WEIGHTS = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "info": 1,
}


class PrimaryPicker:
    def pick(self, violations: list[Any], dag: Any) -> Any | None:
        if not violations:
            return None
        return self._sort_by_dag_severity_timestamp(violations, dag)[0]

    def _sort_by_dag_severity_timestamp(self, violations: list[Any], dag: Any) -> list[Any]:
        levels = _graph_levels(dag)

        def key(pair: tuple[int, Any]) -> tuple[float, int, tuple[int, str], int]:
            index, violation = pair
            return (
                _violation_level(violation, levels),
                -_severity_weight(violation),
                _time_key(violation),
                index,
            )

        return [violation for _, violation in sorted(enumerate(violations), key=key)]


class FirstViolationPicker:
    def pick(self, violations: list[Any], dag: Any = None) -> Any | None:
        return violations[0] if violations else None


def _graph_levels(dag: Any) -> dict[str, int]:
    if dag is None:
        return {}

    node_ids = _node_ids(dag)
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    incoming: dict[str, int] = {node_id: 0 for node_id in node_ids}

    for edge in _edges(dag):
        pair = _edge_pair(edge)
        if pair is None:
            continue
        left, right = pair
        node_ids.update((left, right))
        adjacency.setdefault(left, [])
        adjacency.setdefault(right, [])
        incoming.setdefault(left, 0)
        incoming[right] = incoming.get(right, 0) + 1
        adjacency[left].append(right)

    levels = {node_id: 0 for node_id in node_ids}
    ready = sorted(node_id for node_id in node_ids if incoming.get(node_id, 0) == 0)
    seen: set[str] = set()

    while ready:
        current = ready.pop(0)
        if current in seen:
            continue
        seen.add(current)

        for next_id in sorted(adjacency.get(current, [])):
            levels[next_id] = max(levels.get(next_id, 0), levels[current] + 1)
            incoming[next_id] = incoming.get(next_id, 0) - 1
            if incoming[next_id] == 0:
                ready.append(next_id)
        ready.sort()

    if len(seen) != len(node_ids):
        fallback_level = max((levels[node_id] for node_id in seen), default=-1) + 1
        for node_id in sorted(node_ids - seen):
            levels[node_id] = max(levels.get(node_id, 0), fallback_level)

    return levels


def _violation_level(violation: Any, levels: Mapping[str, int]) -> float:
    values = [levels[node_id] for node_id in _violation_node_ids(violation) if node_id in levels]
    return min(values) if values else inf


def _violation_node_ids(violation: Any) -> list[str]:
    raw = _field(violation, "affected_nodes", "failed_nodes", "node_ids", "nodes")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, Sequence):
        return []

    values: list[str] = []
    for item in raw:
        if isinstance(item, str):
            values.append(item)
            continue
        item_id = _field(item, "id", "node_id", "path", "name")
        if item_id is not None:
            values.append(str(item_id))
    return values


def _severity_weight(violation: Any) -> int:
    raw = _field(violation, "severity")
    if raw is None:
        return 0
    return _SEVERITY_WEIGHTS.get(str(raw).lower(), 0)


def _time_key(violation: Any) -> tuple[int, str]:
    raw = _field(violation, "timestamp", "created_at", "observed_at", "analysis_timestamp")
    if raw is None:
        return (1, "")
    if isinstance(raw, datetime):
        return (0, raw.isoformat())
    return (0, str(raw))


def _node_ids(dag: Any) -> set[str]:
    nodes = _field(dag, "nodes")
    if isinstance(nodes, Mapping):
        return {str(node_id) for node_id in nodes}
    if not isinstance(nodes, Sequence):
        return set()
    return {str(node_id) for node in nodes if (node_id := _field(node, "id")) is not None}


def _edges(dag: Any) -> list[Any]:
    edges = _field(dag, "edges")
    if edges is None:
        return []
    if isinstance(edges, list):
        return edges
    if isinstance(edges, Sequence) and not isinstance(edges, str):
        return list(edges)
    return []


def _edge_pair(edge: Any) -> tuple[str, str] | None:
    left = _field(edge, "from_id", "source", "from")
    right = _field(edge, "to_id", "target", "to")
    if left is None or right is None:
        return None
    return str(left), str(right)


def _field(value: Any, *names: str) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


__all__ = ["FirstViolationPicker", "PrimaryPicker"]
