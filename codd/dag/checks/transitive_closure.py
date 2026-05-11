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
    common_node_count: int = 0
    passed: bool = True


@register_dag_check("transitive_closure")
class TransitiveClosureCheck:
    """Report nodes unreachable from root design docs without blocking deploy.

    Nodes with ``kind == "common"`` (shared infrastructure declared via
    ``common_node_patterns`` or frontmatter ``node_type: common``) are exempt
    from unreachable detection. They participate in the DAG so change-impact
    analysis still sees them, but they do not need to be the descendant of a
    single design document.
    """

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
        common_count = sum(
            1 for node in dag.nodes.values() if node.kind == "common"
        )
        unreachable = [
            node_id
            for node_id, node in dag.nodes.items()
            if node_id not in visited and node.kind != "common"
        ]
        return TransitiveClosureResult(
            unreachable_nodes=unreachable,
            common_node_count=common_count,
            passed=True,
        )

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
