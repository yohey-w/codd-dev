"""Registry for design drift linkers.

Concrete linkers register with ``@register_linker("name")`` and expose a
``run()`` method.  This package intentionally contains only the skeleton so
parallel linker implementations can plug in without touching shared plumbing.
"""

from __future__ import annotations

from typing import Any


_REGISTRY: dict[str, type[Any]] = {}


def register_linker(name: str):
    """Register a linker class under ``name``."""

    def decorator(cls):
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_registry() -> dict[str, type[Any]]:
    """Return a copy of the registered linker mapping."""

    return dict(_REGISTRY)


def run_all_linkers(expected_catalog_path, project_root, settings) -> list[Any]:
    """Instantiate each registered linker and collect its ``run()`` result."""

    results = []
    for cls in _REGISTRY.values():
        linker = cls(expected_catalog_path, project_root, settings)
        results.append(linker.run())
    return results


# Import built-in linkers after registry helpers are defined so decorators run.
try:  # pragma: no cover - import side effect only
    from codd.drift_linkers import screen_flow as _screen_flow  # noqa: F401
except ImportError:
    pass
