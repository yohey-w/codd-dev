"""Registry for deployment completeness checks."""

from __future__ import annotations

from typing import Any


DEPLOYMENT_CHECKS: dict[str, type[Any]] = {}


def register_deployment_check(name: str):
    """Register a deployment check class under ``name``."""

    def decorator(cls):
        DEPLOYMENT_CHECKS[name] = cls
        return cls

    return decorator


__all__ = ["DEPLOYMENT_CHECKS", "register_deployment_check"]
