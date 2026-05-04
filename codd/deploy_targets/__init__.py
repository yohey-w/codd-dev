from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codd.deploy_targets.base import DeployTarget

_registry: dict[str, type["DeployTarget"]] = {}


def register_target(target_type: str):
    def decorator(cls):
        _registry[target_type] = cls
        return cls

    return decorator


def get_target(target_type: str) -> "type[DeployTarget]":
    if target_type not in _registry:
        _load_target_module(target_type)
    if target_type not in _registry:
        from codd.cli import CoddCLIError

        raise CoddCLIError(
            f"Unknown deploy target type: {target_type!r}. "
            f"Registered: {sorted(_registry)}"
        )
    return _registry[target_type]


def list_registered_target_types() -> list[str]:
    return sorted(_registry.keys())


def _load_target_module(target_type: str) -> None:
    module_name = target_type.replace("-", "_")
    qualified_name = f"{__name__}.{module_name}"
    try:
        import_module(qualified_name)
    except ModuleNotFoundError as exc:
        if exc.name != qualified_name:
            raise
