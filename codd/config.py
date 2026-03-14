"""CoDD configuration loader with defaults + project overrides."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import yaml


DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")


def load_project_config(project_root: Path) -> dict[str, Any]:
    """Load CoDD defaults and merge project-local overrides."""
    config_path = project_root / "codd" / "codd.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"{config_path} not found")

    defaults = _read_yaml_mapping(DEFAULTS_PATH)
    project = _read_yaml_mapping(config_path)
    return _deep_merge(defaults, project)


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a YAML mapping")
    return payload


def _deep_merge(defaults: Any, project: Any) -> Any:
    if isinstance(defaults, dict) and isinstance(project, dict):
        merged = deepcopy(defaults)
        for key, value in project.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = deepcopy(value)
        return merged

    if isinstance(defaults, list) and isinstance(project, list):
        return _merge_lists(defaults, project)

    return deepcopy(project)


def _merge_lists(defaults: list[Any], project: list[Any]) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for value in [*defaults, *project]:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if serialized in seen:
            continue
        seen.add(serialized)
        merged.append(deepcopy(value))
    return merged
