"""Registry for DAG completeness checks."""

from __future__ import annotations

from typing import Any


_REGISTRY: dict[str, type[Any]] = {}


def register_dag_check(name: str):
    """Register a DAG check class under ``name``."""

    def decorator(cls):
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_registry() -> dict[str, type[Any]]:
    """Return a copy of the registered DAG check mapping."""

    return dict(_REGISTRY)


def run_all_checks(dag, project_root, settings, check_names: list[str] | tuple[str, ...] | None = None) -> list[Any]:
    """Instantiate each registered DAG check and collect its ``run()`` result."""

    results = []
    selected = list(check_names) if check_names is not None else list(_REGISTRY)
    unknown = [name for name in selected if name not in _REGISTRY]
    if unknown:
        raise ValueError(f"Unknown DAG check(s): {', '.join(unknown)}")
    for name in selected:
        cls = _REGISTRY[name]
        check = cls(dag, project_root, settings)
        results.append(check.run())
    return results
