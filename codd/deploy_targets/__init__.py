from __future__ import annotations

_registry: dict[str, type["DeployTarget"]] = {}


def register_target(target_type: str):
    def decorator(cls):
        _registry[target_type] = cls
        return cls

    return decorator


def get_target(target_type: str) -> "type[DeployTarget]":
    if target_type not in _registry:
        from codd.cli import CoddCLIError

        raise CoddCLIError(
            f"Unknown deploy target type: {target_type!r}. "
            f"Registered: {sorted(_registry)}"
        )
    return _registry[target_type]


def list_registered_target_types() -> list[str]:
    return sorted(_registry.keys())
