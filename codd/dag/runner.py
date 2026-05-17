"""Runner for project-wide DAG completeness checks."""

from __future__ import annotations

from datetime import date
from importlib import import_module
from pathlib import Path
from typing import Any

from codd.config import load_project_config
from codd.dag.builder import build_dag, load_dag_settings
from codd.dag.checks import get_registry
from codd.dag.checks.opt_out import OptOutPolicy


CHECK_MODULES = (
    "codd.dag.checks.node_completeness",
    "codd.dag.checks.edge_validity",
    "codd.dag.checks.depends_on_consistency",
    "codd.dag.checks.task_completion",
    "codd.dag.checks.transitive_closure",
    "codd.dag.checks.ui_coherence",
    "codd.dag.checks.deployment_completeness",
    "codd.dag.checks.user_journey_coherence",
    "codd.dag.checks.ci_health",
    "codd.dag.checks.implementation_coverage",
    "codd.dag.checks.environment_coverage",
)


def run_all_checks(
    project_root: Path,
    settings: dict[str, Any] | None = None,
    check_names: list[str] | tuple[str, ...] | None = None,
    today: date | None = None,
    codd_config: dict[str, Any] | None = None,
) -> list[Any]:
    """Build a DAG for ``project_root`` and run selected registered checks."""

    root = Path(project_root).resolve()
    dag_settings = load_dag_settings(root, settings)
    full_config = _resolve_codd_config(root, settings, codd_config)
    dag = build_dag(root, dag_settings)
    return run_checks(
        dag,
        root,
        dag_settings,
        check_names=check_names,
        today=today,
        codd_config=full_config,
    )


def run_checks(
    dag: Any,
    project_root: Path,
    settings: dict[str, Any] | None = None,
    check_names: list[str] | tuple[str, ...] | None = None,
    today: date | None = None,
    codd_config: dict[str, Any] | None = None,
) -> list[Any]:
    """Run selected registered checks against an already-built DAG."""

    _ensure_checks_registered()
    registry = get_registry()
    effective_settings = settings or {}
    full_config = codd_config if codd_config is not None else effective_settings
    selected_names = _selected_check_names(registry, effective_settings, check_names)
    opt_out_policy = OptOutPolicy.from_config(full_config)
    evaluation_date = today or date.today()

    results: list[Any] = []
    for name in selected_names:
        check_cls = registry[name]
        try:
            check = check_cls(
                dag,
                project_root,
                effective_settings,
                opt_out_policy=opt_out_policy,
                today=evaluation_date,
            )
        except TypeError:
            # Subclass overrides __init__ without policy params; fall back to
            # the legacy signature and attach policy attributes manually.
            check = check_cls(dag, project_root, effective_settings)
            try:
                check.opt_out_policy = opt_out_policy
                check.today = evaluation_date
            except AttributeError:
                pass

        try:
            results.append(check.run(codd_config=full_config))
        except TypeError:
            # Legacy run() signatures that do not accept codd_config; fall
            # back to the no-arg form.
            results.append(check.run())
    return results


def _resolve_codd_config(
    project_root: Path,
    settings: dict[str, Any] | None,
    codd_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the full codd.yaml configuration for non-DAG concerns.

    Order of precedence: explicit ``codd_config`` argument, otherwise the
    provided ``settings`` (treated as full config), otherwise the project's
    ``codd.yaml`` loaded from disk.
    """

    if codd_config is not None:
        return codd_config
    if settings is not None and (
        "ci" in settings or "opt_outs" in settings
    ):
        # Caller passed a full-shaped config (top-level ci/opt_outs present);
        # honour it directly.
        return settings
    try:
        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return settings or {}


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
