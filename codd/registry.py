"""Extractor registry dynamic loader for declared extractors."""

from __future__ import annotations

import importlib
from typing import Any


class RegistryError(Exception):
    """Raised when an extractor registry entry cannot be loaded."""


def load_extractor(entry: dict[str, Any]) -> Any:
    """Dynamically load and instantiate an extractor from a registry entry."""
    type_path = entry.get("type")
    if not type_path:
        raise RegistryError(f"Registry entry missing 'type' field: {entry}")

    parts = type_path.rsplit(".", 1)
    if len(parts) != 2:
        raise RegistryError(f"Invalid type path (expected 'module.ClassName'): {type_path}")

    module_path, class_name = parts
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise RegistryError(f"Cannot import module '{module_path}': {exc}") from exc

    cls = getattr(module, class_name, None)
    if cls is None:
        raise RegistryError(f"Class '{class_name}' not found in module '{module_path}'")

    return cls()


def get_extractor(name: str, registry: dict[str, dict]) -> Any:
    """Look up an extractor by name and instantiate it, or return None."""
    entry = registry.get(name)
    if entry is None:
        return None
    return load_extractor(entry)


def list_extractors(registry: dict[str, dict]) -> list[str]:
    """Return registered extractor names."""
    return list(registry.keys())
