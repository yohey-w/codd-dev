"""Project-aware verification means catalog resolution for LLM prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Mapping

import yaml

from codd.deployment.providers.verification.means_catalog import VerificationMeansCatalog


class MeansCatalogLoader:
    """Resolve catalog overrides and render them as prompt hint text."""

    DEFAULT_CATALOG_PATH: ClassVar[str] = "codd/llm/templates/verification_means_catalog.yaml"

    def resolve(
        self,
        project_lexicon_path: str | None = None,
        codd_yaml_path: str | None = None,
        *,
        project_lexicon_catalog: Mapping[str, Any] | None = None,
    ) -> dict[str, list[str]]:
        """Resolve project lexicon override, config override, then bundled default."""

        if project_lexicon_catalog is not None:
            return _normalize_catalog(project_lexicon_catalog)

        lexicon_catalog = _catalog_from_project_lexicon(_path_or_none(project_lexicon_path))
        if lexicon_catalog is not None:
            return lexicon_catalog

        config_catalog_path = _catalog_path_from_codd_yaml(_path_or_none(codd_yaml_path))
        if config_catalog_path is not None:
            return VerificationMeansCatalog.load(config_catalog_path)

        return VerificationMeansCatalog.load(_default_catalog_path())

    @staticmethod
    def to_hint_text(catalog: dict[str, list[str]]) -> str:
        """Render a catalog as YAML suitable for inclusion in an LLM prompt."""

        return yaml.safe_dump(catalog, sort_keys=True, allow_unicode=True)


def _catalog_from_project_lexicon(path: Path | None) -> dict[str, list[str]] | None:
    if path is None or not path.exists():
        return None
    payload = _read_yaml_mapping(path)
    catalog = payload.get("verification_means_catalog")
    if catalog is None:
        return None
    return _normalize_catalog(catalog)


def _catalog_path_from_codd_yaml(path: Path | None) -> Path | None:
    if path is None or not path.exists():
        return None
    payload = _read_yaml_mapping(path)
    configured = _nested_value(payload, ("llm", "verification_means_catalog_path"))
    if not isinstance(configured, str) or not configured.strip():
        configured = _nested_value(payload, ("llm", "means_catalog_path"))
    if not isinstance(configured, str) or not configured.strip():
        return None
    return _resolve_config_relative_path(Path(configured), path)


def _resolve_config_relative_path(path: Path, codd_yaml_path: Path) -> Path:
    if path.is_absolute():
        return path
    config_dir = codd_yaml_path.parent
    config_relative = config_dir / path
    if config_relative.exists():
        return config_relative
    if config_dir.name in {"codd", ".codd"}:
        return config_dir.parent / path
    return config_relative


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a YAML mapping")
    return payload


def _normalize_catalog(payload: Any) -> dict[str, list[str]]:
    if not isinstance(payload, Mapping):
        raise ValueError("verification means catalog must be a mapping")
    if "catalog" in payload and isinstance(payload["catalog"], Mapping):
        payload = payload["catalog"]
    normalized: dict[str, list[str]] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            normalized[str(key)] = [value]
            continue
        if isinstance(value, list):
            normalized[str(key)] = [str(item) for item in value]
            continue
        raise ValueError(f"catalog entry must be a list or string: {key}")
    return normalized


def _nested_value(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def _path_or_none(path: str | None) -> Path | None:
    return Path(path) if path else None


def _default_catalog_path() -> Path:
    return Path(__file__).parents[2] / MeansCatalogLoader.DEFAULT_CATALOG_PATH


__all__ = ["MeansCatalogLoader"]
