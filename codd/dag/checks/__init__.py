"""Registry for DAG completeness checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


_REGISTRY: dict[str, type[Any]] = {}


@dataclass
class CheckResult:
    """Generic DAG check result for lightweight scaffold checks."""

    check_name: str = "dag_check"
    severity: str = "red"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = True
    passed: bool | None = None

    def __post_init__(self) -> None:
        if self.passed is None:
            self.passed = self.status.lower() in {"pass", "passed", "ok", "skip", "skipped"}


class DagCheck:
    """Base class for DAG checks that keep runner-provided context."""

    def __init__(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.dag = dag
        self.project_root = Path(project_root) if project_root is not None else None
        self.settings = settings or {}

    def run(self, dag: Any | None = None) -> CheckResult:
        raise NotImplementedError


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
