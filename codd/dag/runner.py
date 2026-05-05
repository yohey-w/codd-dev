"""Runner for project-wide DAG completeness checks."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from codd.dag.builder import build_dag, load_dag_settings
from codd.dag.checks import get_registry


CHECK_MODULES = (
    "codd.dag.checks.node_completeness",
    "codd.dag.checks.edge_validity",
    "codd.dag.checks.depends_on_consistency",
    "codd.dag.checks.task_completion",
    "codd.dag.checks.transitive_closure",
    "codd.dag.checks.deployment_completeness",
)


def run_all_checks(
    project_root: Path,
    settings: dict[str, Any] | None = None,
    check_names: list[str] | tuple[str, ...] | None = None,
) -> list[Any]:
    """Build a DAG for ``project_root`` and run selected registered checks."""

    root = Path(project_root).resolve()
    dag_settings = load_dag_settings(root, settings)
    dag = build_dag(root, dag_settings)
    return run_checks(dag, root, dag_settings, check_names=check_names)


def run_checks(
    dag: Any,
    project_root: Path,
    settings: dict[str, Any] | None = None,
    check_names: list[str] | tuple[str, ...] | None = None,
) -> list[Any]:
    """Run selected registered checks against an already-built DAG."""

    _ensure_checks_registered()
    registry = get_registry()
    selected_names = _selected_check_names(registry, settings or {}, check_names)

    results: list[Any] = []
    for name in selected_names:
        check_cls = registry[name]
        check = check_cls(dag, project_root, settings or {})
        results.append(check.run())
    return results


def _ensure_checks_registered() -> None:
    for module_name in CHECK_MODULES:
        import_module(module_name)


def _selected_check_names(
    registry: dict[str, type[Any]],
    settings: dict[str, Any],
    check_names: list[str] | tuple[str, ...] | None,
) -> list[str]:
    requested = list(check_names) if check_names is not None else settings.get("enabled_checks")
    if requested is None:
        requested = list(registry)

    selected = [str(name) for name in requested]
    unknown = [name for name in selected if name not in registry]
    if unknown:
        available = ", ".join(sorted(registry)) or "(none)"
        raise ValueError(f"Unknown DAG check(s): {', '.join(unknown)}. Available: {available}")
    return selected
