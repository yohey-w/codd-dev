"""Validate that DAG edges and node file references are well-formed."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag import DAG
from codd.dag.checks import register_dag_check


@dataclass
class EdgeValidityResult:
    check_name: str = "edge_validity"
    severity: str = "red"
    orphan_edges: list[dict[str, str]] = field(default_factory=list)
    dangling_refs: list[str] = field(default_factory=list)
    passed: bool = True


@register_dag_check("edge_validity")
class EdgeValidityCheck:
    """Detect edges whose endpoint nodes are absent and nodes pointing to missing files."""

    def __init__(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ):
        self.dag = dag
        self.project_root = Path(project_root) if project_root is not None else None
        self.settings = settings or {}

    def run(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> EdgeValidityResult:
        target_dag = dag or self.dag
        if target_dag is None:
            raise ValueError("dag is required for edge_validity check")

        root = Path(project_root) if project_root is not None else self.project_root
        root = root or Path(".")
        _ = settings or self.settings

        orphan_edges = [
            {"from_id": edge.from_id, "to_id": edge.to_id, "kind": edge.kind}
            for edge in target_dag.edges
            if edge.from_id not in target_dag.nodes or edge.to_id not in target_dag.nodes
        ]
        dangling_refs = [
            node_id
            for node_id, node in sorted(target_dag.nodes.items())
            if node.path and not _node_path_exists(root, node.path)
        ]

        return EdgeValidityResult(
            orphan_edges=orphan_edges,
            dangling_refs=dangling_refs,
            passed=not orphan_edges and not dangling_refs,
        )


def _node_path_exists(project_root: Path, node_path: str) -> bool:
    path = Path(node_path)
    candidate = path if path.is_absolute() else project_root / path
    return candidate.exists()
