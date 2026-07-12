"""Runner for project-wide DAG completeness checks."""

from __future__ import annotations

import inspect
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import Any

from codd.config import load_project_config
from codd.dag.builder import build_dag, load_dag_settings
from codd.dag.checks import CheckResult, get_registry
from codd.dag.checks.opt_out import OptOutPolicy


CHECK_MODULES = (
    "codd.dag.checks.node_completeness",
    "codd.dag.checks.edge_validity",
    "codd.dag.checks.depends_on_consistency",
    "codd.dag.checks.dependency_freshness",
    "codd.dag.checks.task_completion",
    "codd.dag.checks.transitive_closure",
    "codd.dag.checks.ui_coherence",
    "codd.dag.checks.deployment_completeness",
    "codd.dag.checks.user_journey_coherence",
    "codd.dag.checks.ci_health",
    "codd.dag.checks.implementation_coverage",
    "codd.dag.checks.environment_coverage",
    "codd.dag.checks.artifact_contract_check",
    "codd.dag.checks.resource_flow_coherence",
    "codd.dag.checks.extraction_diagnostics",
    "codd.dag.checks.cardinality_coverage",
    "codd.dag.checks.stale_evidence",
    "codd.dag.checks.negative_space",
    "codd.dag.checks.semantic_contract_conflict",
    "codd.dag.checks.source_completeness",
    "codd.dag.checks.unresolved_import_residue",
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
    try:
        dag = build_dag(root, dag_settings)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        # A DAG that could not be built verified nothing — surface it as a red
        # result on the SAME result path the gate reads, rather than letting the
        # exception propagate as an uncaught crash (or, worse, be swallowed by a
        # caller's broad ``except`` into a silent no-op). Anti-false-green.
        return [_error_result("dag_build", exc)]
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
            check = _construct_check(
                check_cls,
                dag,
                project_root,
                effective_settings,
                opt_out_policy,
                evaluation_date,
            )
            result = _run_check(check, full_config)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            # A check that raised (constructor OR run) verified nothing reliable.
            # Convert it to a red result on the shared result path instead of
            # letting the exception crash the run or — for an internal
            # ``TypeError`` — be mistaken for a legacy call form and silently
            # re-run into a green PASS. Anti-false-green.
            result = _error_result(name, exc)
        else:
            if result is None:
                # A check that returned no verdict must not read as a clean pass.
                result = _error_result(name, None, message="check returned no result")
        results.append(result)
    return results


def _construct_check(
    check_cls: type[Any],
    dag: Any,
    project_root: Path,
    settings: dict[str, Any],
    opt_out_policy: Any,
    today: date,
) -> Any:
    """Instantiate ``check_cls``, choosing the call form by signature.

    The policy-aware signature is ``(dag, project_root, settings, opt_out_policy,
    today)``; legacy checks override ``__init__`` without the policy params.
    Capability is decided by ``inspect.signature(...).bind`` BEFORE construction
    — never by catching a ``TypeError`` (which would also swallow a genuine
    constructor-internal ``TypeError``).
    """
    policy_kwargs = {"opt_out_policy": opt_out_policy, "today": today}
    if _accepts(check_cls, (dag, project_root, settings), policy_kwargs):
        return check_cls(dag, project_root, settings, **policy_kwargs)
    check = check_cls(dag, project_root, settings)
    for attr, value in (("opt_out_policy", opt_out_policy), ("today", today)):
        try:
            setattr(check, attr, value)
        except AttributeError:
            pass
    return check


def _run_check(check: Any, codd_config: dict[str, Any]) -> Any:
    """Invoke ``check.run`` exactly once, passing ``codd_config`` only when the
    run signature accepts it (decided by signature, not by ``TypeError``)."""
    run = check.run
    if _accepts(run, (), {"codd_config": codd_config}):
        return run(codd_config=codd_config)
    return run()


def _accepts(target: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    """True iff ``target`` (a callable/class) can bind ``args``/``kwargs``.

    Falls back to ``True`` when the signature cannot be introspected (e.g. a
    C-level callable), so an un-introspectable check is still attempted rather
    than being forced onto the legacy path.
    """
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return True
    try:
        signature.bind(*args, **kwargs)
    except TypeError:
        return False
    return True


def _error_result(check_name: str, exc: BaseException | None, message: str | None = None) -> CheckResult:
    """Build a red ``CheckResult`` for a check that raised or produced no verdict."""
    if message is None:
        message = f"{type(exc).__name__}: {exc}" if exc is not None else "check error"
    return CheckResult(
        check_name=check_name,
        severity="red",
        status="error",
        message=message,
        passed=False,
    )


def unselected_check_names(
    project_root: Path,
    settings: dict[str, Any] | None = None,
) -> list[str]:
    """Registered DAG checks that the effective ``enabled_checks`` does not select.

    ``enabled_checks`` (from the project-type defaults or a ``codd.yaml``
    ``dag:`` override) is an explicit allowlist: when present, checks shipped
    after the list was written silently never run. This helper powers the
    ``codd dag verify`` notice that keeps that gap visible instead of letting
    it silently no-op. Returns an empty list when no allowlist is in effect
    (all registered checks run) or when settings cannot be resolved.
    """

    _ensure_checks_registered()
    registry = get_registry()
    try:
        dag_settings = load_dag_settings(Path(project_root).resolve(), settings)
    except (FileNotFoundError, ValueError, OSError):
        return []
    requested = dag_settings.get("enabled_checks")
    if not isinstance(requested, (list, tuple)):
        return []
    selected = {str(name) for name in requested}
    return sorted(name for name in registry if name not in selected)


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
