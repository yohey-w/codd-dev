"""Core DAG primitives for completeness checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Node:
    id: str
    kind: str
    path: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    from_id: str
    to_id: str
    kind: str
    attributes: dict[str, Any] = field(default_factory=dict)


class DAG:
    def __init__(self):
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []

    def add_node(self, node: Node) -> None:
        if node.id in self.nodes:
            raise ValueError(f"duplicate DAG node id: {node.id}")
        self.nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)

    def detect_cycles(self) -> list[list[str]]:
        """Return strongly connected components that form directed cycles."""

        adjacency = self._adjacency()
        index = 0
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        cycles: list[list[str]] = []

        def visit(node_id: str) -> None:
            nonlocal index
            indices[node_id] = index
            lowlinks[node_id] = index
            index += 1
            stack.append(node_id)
            on_stack.add(node_id)

            for neighbor in adjacency.get(node_id, []):
                if neighbor not in indices:
                    visit(neighbor)
                    lowlinks[node_id] = min(lowlinks[node_id], lowlinks[neighbor])
                elif neighbor in on_stack:
                    lowlinks[node_id] = min(lowlinks[node_id], indices[neighbor])

            if lowlinks[node_id] != indices[node_id]:
                return

            component = []
            while stack:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node_id:
                    break

            if len(component) > 1 or node_id in adjacency.get(node_id, []):
                cycles.append(sorted(component))

        for node_id in sorted(adjacency):
            if node_id not in indices:
                visit(node_id)

        return cycles

    def get_neighbors(self, node_id: str) -> list[str]:
        return [edge.to_id for edge in self.edges if edge.from_id == node_id]

    def reverse_closure(self, node_id: str) -> set[str]:
        """Return all nodes that can reach ``node_id`` by following edges backward."""

        reverse: dict[str, list[str]] = {}
        for edge in self.edges:
            reverse.setdefault(edge.to_id, []).append(edge.from_id)

        visited: set[str] = set()
        stack = list(reverse.get(node_id, []))
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            stack.extend(reverse.get(current, []))
        return visited

    def _adjacency(self) -> dict[str, list[str]]:
        node_ids = set(self.nodes)
        for edge in self.edges:
            node_ids.add(edge.from_id)
            node_ids.add(edge.to_id)

        adjacency = {node_id: [] for node_id in node_ids}
        for edge in self.edges:
            adjacency.setdefault(edge.from_id, []).append(edge.to_id)
        return adjacency
