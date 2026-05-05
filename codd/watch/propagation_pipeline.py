"""Change-driven propagation pipeline for CDAP."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from codd.config import find_codd_dir
from codd.watch.events import FileChangeEvent
from codd.watch.propagation_log import append_propagation_log


@dataclass
class PropagationResult:
    """Result of a full propagation pipeline run."""

    impacted_nodes: list[str] | None = None
    propagated_count: int = 0
    fixed_count: int = 0
    drift_events: list[Any] | None = None
    errors: list[str] | None = None
    success: bool = True
    verified_count: int = 0
    log_written: bool = False
    event_published: bool = False

    def __post_init__(self) -> None:
        self.impacted_nodes = list(self.impacted_nodes or [])
        self.drift_events = list(self.drift_events or [])
        self.errors = list(self.errors or [])


def run_propagation_pipeline(
    project_root: Path,
    files: list[str],
    settings: dict[str, Any] | None = None,
    dry_run: bool = False,
    event: FileChangeEvent | None = None,
) -> PropagationResult:
    """Run impact -> propagate -> verify -> fix -> drift for changed files."""

    result = PropagationResult()
    root = Path(project_root).resolve()
    pipeline_settings = settings or {}
    event = event or FileChangeEvent(files=list(files), source="manual")

    try:
        from codd.dag.builder import build_dag

        dag = build_dag(root, pipeline_settings.get("dag"))
        result.impacted_nodes = _impacted_nodes(dag, root, files)
    except Exception as exc:
        result.errors.append(f"impact: {exc}")
        result.success = False
        return result

    if dry_run:
        return result

    _publish_file_change_event(event, pipeline_settings, result)

    if result.impacted_nodes:
        _run_propagate_step(root, pipeline_settings, result)
    _run_verify_step(root, pipeline_settings, result)
    _run_fix_step(root, pipeline_settings, result)
    _run_drift_step(root, result)

    result.success = not result.errors
    _write_log(root, event, result)
    return result


def _impacted_nodes(dag: Any, project_root: Path, files: list[str]) -> list[str]:
    changed_node_ids = _changed_node_ids(dag, project_root, files)
    impacted = set(changed_node_ids)
    for node_id in changed_node_ids:
        reverse_closure = getattr(dag, "reverse_closure", None)
        if callable(reverse_closure):
            impacted.update(reverse_closure(node_id))
    return sorted(impacted)


def _changed_node_ids(dag: Any, project_root: Path, files: list[str]) -> list[str]:
    normalized_files = {_normalize_changed_file(project_root, item) for item in files if str(item).strip()}
    if not normalized_files:
        return []

    changed = []
    for node_id, node in getattr(dag, "nodes", {}).items():
        node_path = getattr(node, "path", None)
        candidates = {str(node_id)}
        if node_path:
            candidates.add(str(node_path))
        if any(_paths_match(candidate, changed_file) for candidate in candidates for changed_file in normalized_files):
            changed.append(str(node_id))
    return sorted(changed)


def _normalize_changed_file(project_root: Path, value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(project_root).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix().lstrip("./")


def _paths_match(candidate: str, changed_file: str) -> bool:
    normalized_candidate = Path(candidate).as_posix().lstrip("./")
    normalized_changed = Path(changed_file).as_posix().lstrip("./")
    return (
        normalized_candidate == normalized_changed
        or normalized_candidate.endswith(f"/{normalized_changed}")
        or normalized_changed.endswith(f"/{normalized_candidate}")
    )


def _publish_file_change_event(
    event: FileChangeEvent,
    settings: dict[str, Any],
    result: PropagationResult,
) -> None:
    event_bus = settings.get("event_bus")
    if event_bus is None:
        return
    try:
        event_bus.publish(event)
        result.event_published = True
    except Exception as exc:
        result.errors.append(f"event_bus: {exc}")


def _run_propagate_step(project_root: Path, settings: dict[str, Any], result: PropagationResult) -> None:
    try:
        from codd.propagator import run_propagate

        propagation = run_propagate(
            project_root,
            diff_target=str(settings.get("diff_target", "HEAD")),
            update=bool(settings.get("propagate_update", False)),
            ai_command=settings.get("ai_command"),
            feedback=settings.get("feedback"),
            coherence_context=settings.get("coherence_context"),
        )
        result.propagated_count = _count_propagated(propagation)
    except Exception as exc:
        result.errors.append(f"propagate: {exc}")


def _count_propagated(propagation: Any) -> int:
    updated = getattr(propagation, "updated", None)
    if updated is not None:
        return len(updated)
    affected_docs = getattr(propagation, "affected_docs", None)
    if affected_docs is not None:
        return len(affected_docs)
    return 1 if propagation else 0


def _run_verify_step(project_root: Path, settings: dict[str, Any], result: PropagationResult) -> None:
    try:
        from codd.dag.runner import run_all_checks

        checks = run_all_checks(project_root, settings=settings.get("dag"), check_names=settings.get("check_names"))
        result.verified_count = len(checks)
    except Exception as exc:
        result.errors.append(f"verify: {exc}")


def _run_fix_step(project_root: Path, settings: dict[str, Any], result: PropagationResult) -> None:
    try:
        from codd.fixer import run_fix

        fix_result = run_fix(
            project_root,
            ai_command=settings.get("ai_command"),
            max_attempts=int(settings.get("max_fix_attempts", 1)),
            local_only=bool(settings.get("fix_local_only", True)),
            push=False,
            dry_run=bool(settings.get("fix_dry_run", False)),
        )
        result.fixed_count = _count_fixed(fix_result)
    except Exception as exc:
        result.errors.append(f"fix: {exc}")


def _count_fixed(fix_result: Any) -> int:
    fixed_count = getattr(fix_result, "fixed_count", None)
    if fixed_count is not None:
        return int(fixed_count)
    return 1 if getattr(fix_result, "fixed", False) and getattr(fix_result, "attempts", []) else 0


def _run_drift_step(project_root: Path, result: PropagationResult) -> None:
    try:
        from codd.drift import run_drift

        codd_dir = find_codd_dir(project_root)
        if codd_dir is None:
            return
        drift_result = run_drift(project_root, codd_dir)
        result.drift_events = [_serializable(item) for item in getattr(drift_result, "drift", [])]
    except Exception as exc:
        result.errors.append(f"drift: {exc}")


def _write_log(project_root: Path, event: FileChangeEvent, result: PropagationResult) -> None:
    append_propagation_log(
        project_root,
        event,
        {
            "impacted_nodes": result.impacted_nodes,
            "propagated": result.propagated_count,
            "verified": result.verified_count,
            "fixed": result.fixed_count,
            "residual_drift": result.drift_events,
            "errors": result.errors,
            "success": result.success,
        },
    )
    result.log_written = True


def _serializable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    return value
