"""Transitive closure DAG completeness check."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag.checks import register_dag_check


@dataclass
class TransitiveClosureResult:
    check_name: str = "transitive_closure"
    severity: str = "amber"
    unreachable_nodes: list[str] = field(default_factory=list)
    passed: bool = True


@register_dag_check("transitive_closure")
class TransitiveClosureCheck:
    """Report nodes unreachable from root design docs without blocking deploy."""

    def __init__(self, dag=None, project_root: Path | None = None, settings: dict[str, Any] | None = None):
        self.dag = dag
        self.project_root = project_root
        self.settings = settings or {}

    def run(
        self,
        dag=None,
        project_root: Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> TransitiveClosureResult:
        dag = dag or self.dag
        if dag is None or not dag.nodes:
            return TransitiveClosureResult()

        to_ids = {edge.to_id for edge in dag.edges}
        roots = [
            node.id
            for node in dag.nodes.values()
            if node.kind == "design_doc" and node.id not in to_ids
        ]

        visited = self._reachable_from(dag, roots)
        unreachable = [node_id for node_id in dag.nodes if node_id not in visited]
        return TransitiveClosureResult(unreachable_nodes=unreachable, passed=True)

    def _reachable_from(self, dag, roots: list[str]) -> set[str]:
        visited: set[str] = set()
        queue = deque(roots)

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for edge in dag.edges:
                if edge.from_id == current and edge.to_id not in visited:
                    queue.append(edge.to_id)

        return visited
