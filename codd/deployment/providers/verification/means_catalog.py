"""Verification means catalog loader."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping

import yaml

from codd.config import load_project_config


class VerificationMeansCatalog:
    """Load verification means from project override or bundled defaults."""

    DEFAULT_CATALOG_PATH: ClassVar[str] = str(
        Path(__file__).parents[2] / "defaults" / "verification_means_catalog.yaml"
    )

    @classmethod
    def load(
        cls,
        override_path: str | Path | None = None,
        *,
        project_root: str | Path | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, list[str]]:
        """Load the catalog as ``domain -> means``."""

        root = Path(project_root) if project_root is not None else None
        resolved_config = _load_config(root, config)
        path = _resolve_catalog_path(
            override_path=override_path,
            project_root=root,
            config=resolved_config,
            default_path=Path(cls.DEFAULT_CATALOG_PATH),
        )
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return _normalize_catalog(payload)


def _resolve_catalog_path(
    *,
    override_path: str | Path | None,
    project_root: Path | None,
    config: Mapping[str, Any] | None,
    default_path: Path,
) -> Path:
    if override_path is not None:
        return _resolve_path(Path(override_path), project_root)
    configured = _nested_value(config, ("verification", "means_catalog_path"))
    if isinstance(configured, str) and configured.strip():
        return _resolve_path(Path(configured), project_root)
    return default_path


def _resolve_path(path: Path, project_root: Path | None) -> Path:
    if path.is_absolute() or project_root is None:
        return path
    return project_root / path


def _load_config(
    project_root: Path | None,
    config: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if config is not None:
        return config
    if project_root is None:
        return None
    try:
        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return None


def _normalize_catalog(payload: Any) -> dict[str, list[str]]:
    if not isinstance(payload, Mapping):
        raise ValueError("verification means catalog must be a mapping")
    if "catalog" in payload and isinstance(payload["catalog"], Mapping):
        payload = payload["catalog"]
    normalized: dict[str, list[str]] = {}
    for domain, means in payload.items():
        if isinstance(means, str):
            normalized[str(domain)] = [means]
        elif isinstance(means, list):
            normalized[str(domain)] = [str(item) for item in means]
        else:
            raise ValueError(f"catalog entry must be a list or string: {domain}")
    return normalized


def _nested_value(config: Mapping[str, Any] | None, path: tuple[str, ...]) -> Any:
    value: Any = config
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


__all__ = ["VerificationMeansCatalog"]
