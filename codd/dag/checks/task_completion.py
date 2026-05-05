"""Task completion check for DAG plan tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag.checks import register_dag_check


@dataclass
class IncompleteTask:
    task_id: str
    missing_outputs: list[str]
    reason: str


@dataclass
class TaskCompletionResult:
    check_name: str = "task_completion"
    severity: str = "red"
    incomplete_tasks: list[IncompleteTask] = field(default_factory=list)
    total_tasks: int = 0
    completed_tasks: int = 0
    completion_rate: float = 1.0
    passed: bool = True


@register_dag_check("task_completion")
class TaskCompletionCheck:
    """Verify that plan tasks produce implementation files that actually exist."""

    def __init__(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.dag = dag
        self.project_root = Path(project_root) if project_root is not None else None
        self.settings = settings or {}

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> TaskCompletionResult:
        active_dag = dag if dag is not None else self.dag
        if active_dag is None:
            raise ValueError("dag is required")

        active_root = self._project_root(project_root)
        active_settings = self._settings(settings)
        incomplete: list[IncompleteTask] = []

        plan_task_nodes = [node for node in active_dag.nodes.values() if node.kind == "plan_task"]
        for task_node in plan_task_nodes:
            produces_edges = [
                edge for edge in active_dag.edges if edge.from_id == task_node.id and edge.kind == "produces"
            ]
            if not produces_edges:
                incomplete.append(
                    IncompleteTask(task_id=task_node.id, missing_outputs=[], reason="no_produces_edge")
                )
                continue

            missing_outputs = []
            drifted_outputs = []
            for edge in produces_edges:
                impl_node = active_dag.nodes.get(edge.to_id)
                if impl_node is None:
                    missing_outputs.append(edge.to_id)
                    continue
                if not self._impl_file_exists(impl_node, edge.to_id, active_root):
                    missing_outputs.append(edge.to_id)
                    continue
                if self._has_drift(impl_node):
                    drifted_outputs.append(edge.to_id)

            if missing_outputs:
                incomplete.append(
                    IncompleteTask(
                        task_id=task_node.id,
                        missing_outputs=missing_outputs,
                        reason="file_missing",
                    )
                )
            elif drifted_outputs:
                incomplete.append(
                    IncompleteTask(
                        task_id=task_node.id,
                        missing_outputs=drifted_outputs,
                        reason="drift_detected",
                    )
                )

        total = len(plan_task_nodes)
        completed = total - len(incomplete)
        completion_rate = completed / total if total else 1.0
        threshold = self._threshold(active_settings)

        return TaskCompletionResult(
            incomplete_tasks=incomplete,
            total_tasks=total,
            completed_tasks=completed,
            completion_rate=completion_rate,
            passed=completion_rate >= threshold,
        )

    def _project_root(self, project_root: str | Path | None) -> Path | None:
        if project_root is not None:
            return Path(project_root)
        return self.project_root

    def _settings(self, settings: dict[str, Any] | None) -> dict[str, Any]:
        if settings is None:
            return self.settings
        return settings

    def _impl_file_exists(self, impl_node: Any, fallback_path: str, project_root: Path | None) -> bool:
        if project_root is None:
            return True
        node_path = getattr(impl_node, "path", None) or fallback_path
        candidate = Path(node_path)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        return candidate.is_file()

    def _has_drift(self, impl_node: Any) -> bool:
        attributes = getattr(impl_node, "attributes", {}) or {}
        if attributes.get("has_drift") is True or attributes.get("drift_detected") is True:
            return True
        if attributes.get("status") == "drift":
            return True
        if self._positive_count(attributes.get("drift_count")):
            return True
        for key in ("drift", "drifts", "violations"):
            if self._non_empty(attributes.get(key)):
                return True
        return False

    def _positive_count(self, value: Any) -> bool:
        try:
            return int(value) > 0
        except (TypeError, ValueError):
            return False

    def _non_empty(self, value: Any) -> bool:
        if value is None or value is False:
            return False
        if isinstance(value, (list, tuple, set, dict, str)):
            return len(value) > 0
        return bool(value)

    def _threshold(self, settings: dict[str, Any]) -> float:
        value = settings.get("task_completion_threshold", 1.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 1.0

