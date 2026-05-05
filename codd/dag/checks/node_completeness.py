"""Node completeness check for DAG expected implementation files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag.checks import register_dag_check


@dataclass
class NodeCompletenessResult:
    check_name: str = "node_completeness"
    severity: str = "red"
    missing_impl_files: list[str] = field(default_factory=list)
    passed: bool = True


@register_dag_check("node_completeness")
class NodeCompletenessCheck:
    """Verify that every ``expects`` edge points to an existing impl file."""

    def __init__(self, dag=None, project_root=None, settings: dict[str, Any] | None = None):
        self.dag = dag
        self.project_root = Path(project_root) if project_root is not None else None
        self.settings = settings or {}

    def run(self, dag=None, project_root=None, settings: dict[str, Any] | None = None) -> NodeCompletenessResult:
        dag = dag if dag is not None else self.dag
        if dag is None:
            raise ValueError("dag is required")

        root = Path(project_root) if project_root is not None else self.project_root
        if root is None:
            root = Path.cwd()

        missing: list[str] = []
        seen: set[str] = set()

        for edge in dag.edges:
            if edge.kind != "expects":
                continue

            node = dag.nodes.get(edge.to_id)
            if node is None or node.kind != "impl_file":
                _append_once(missing, seen, edge.to_id)
                continue

            if node.path and not (root / node.path).exists():
                _append_once(missing, seen, edge.to_id)

        return NodeCompletenessResult(
            missing_impl_files=missing,
            passed=not missing,
        )


def _append_once(items: list[str], seen: set[str], value: str) -> None:
    if value in seen:
        return
    seen.add(value)
    items.append(value)
