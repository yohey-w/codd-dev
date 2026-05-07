"""List and install bundled lexicon plug-ins."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codd.init.lexicon_suggest import append_suggested_lexicons, default_lexicon_root
from codd.lexicon import LEXICON_FILENAME, load_lexicon as load_project_lexicon


@dataclass(frozen=True)
class LexiconRecord:
    id: str
    lexicon_name: str
    description: str
    observation_dimensions: int
    path: Path
    installed: bool = False
    recommended_kinds: tuple[str, ...] = ()
    has_coverage_axes: bool = False


@dataclass(frozen=True)
class InstallResult:
    project_lexicon_path: Path
    installed: tuple[str, ...]
    skipped: tuple[str, ...]
    records: tuple[LexiconRecord, ...]


class LexiconManager:
    """Data-driven access to bundled lexicons and project installation state."""

    def __init__(self, project_root: Path | str, lexicon_root: Path | str | None = None):
        self.project_root = Path(project_root).resolve()
        self.lexicon_root = Path(lexicon_root).resolve() if lexicon_root is not None else default_lexicon_root()

    def installed_ids(self) -> list[str]:
        path = self.project_root / LEXICON_FILENAME
        if not path.is_file():
            return []

        data: dict[str, Any] | None = None
        try:
            loaded = load_project_lexicon(self.project_root)
            data = loaded.as_dict() if loaded is not None else None
        except Exception:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(payload, dict):
                data = payload
        if not data:
            return []

        raw = data.get("suggested_lexicons", [])
        if not isinstance(raw, list):
            return []
        return _dedupe(_lexicon_id(item) for item in raw)

    def available(self) -> list[LexiconRecord]:
        installed = set(self.installed_ids())
        if not self.lexicon_root.is_dir():
            return []

        records: list[LexiconRecord] = []
        for lexicon_dir in sorted(path for path in self.lexicon_root.iterdir() if path.is_dir()):
            manifest_path = lexicon_dir / "manifest.yaml"
            if not manifest_path.is_file():
                continue
            manifest = _load_yaml_mapping(manifest_path)
            records.append(_record_from_manifest(lexicon_dir, manifest, installed))
        return records

    def installed(self) -> list[LexiconRecord]:
        return [record for record in self.available() if record.installed]

    def uninstalled(self) -> list[LexiconRecord]:
        return [record for record in self.available() if not record.installed]

    def resolve(self, lexicon_id: str) -> LexiconRecord:
        key = lexicon_id.strip()
        if not key:
            raise ValueError("lexicon id must be non-empty")
        for record in self.available():
            if key in {record.id, record.lexicon_name}:
                return record
        raise ValueError(f"Unknown lexicon: {lexicon_id}")

    def install(self, lexicon_ids: list[str] | tuple[str, ...]) -> InstallResult:
        records = tuple(self.resolve(lexicon_id) for lexicon_id in lexicon_ids)
        before = set(self.installed_ids())
        ids_to_store = [record.id for record in records]
        project_lexicon_path = append_suggested_lexicons(self.project_root, ids_to_store)

        installed: list[str] = []
        skipped: list[str] = []
        for record in records:
            if record.id in before:
                skipped.append(record.id)
            else:
                installed.append(record.id)

        refreshed = tuple(self.resolve(record.id) for record in records)
        return InstallResult(
            project_lexicon_path=project_lexicon_path,
            installed=tuple(installed),
            skipped=tuple(skipped),
            records=refreshed,
        )


def _record_from_manifest(lexicon_dir: Path, manifest: dict[str, Any], installed: set[str]) -> LexiconRecord:
    lexicon_id = lexicon_dir.name
    lexicon_name = _optional_str(manifest.get("lexicon_name")) or _optional_str(manifest.get("name")) or lexicon_id
    lexicon_yaml = _lexicon_yaml_path(lexicon_dir, manifest)
    axes = _load_coverage_axes(lexicon_yaml, manifest)
    observation_dimensions = _int_or_default(manifest.get("observation_dimensions"), len(axes))
    return LexiconRecord(
        id=lexicon_id,
        lexicon_name=lexicon_name,
        description=_optional_str(manifest.get("description")) or "",
        observation_dimensions=observation_dimensions,
        path=lexicon_dir,
        installed=lexicon_id in installed or lexicon_name in installed,
        recommended_kinds=tuple(_load_recommended_kinds(lexicon_dir, manifest)),
        has_coverage_axes=bool(axes),
    )


def _load_coverage_axes(lexicon_yaml: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if lexicon_yaml.is_file():
        payload = _load_yaml_mapping(lexicon_yaml)
        raw_axes = payload.get("coverage_axes", [])
    else:
        raw_axes = manifest.get("coverage_axes", [])
    if not isinstance(raw_axes, list):
        return []
    return [axis for axis in raw_axes if isinstance(axis, dict)]


def _lexicon_yaml_path(lexicon_dir: Path, manifest: dict[str, Any]) -> Path:
    declared = _optional_str(manifest.get("lexicon")) or "lexicon.yaml"
    path = Path(declared)
    return path if path.is_absolute() else lexicon_dir / path


def _load_recommended_kinds(lexicon_dir: Path, manifest: dict[str, Any]) -> list[str]:
    declared = _optional_str(manifest.get("recommended_kinds"))
    if declared is None:
        return []
    path = Path(declared)
    if not path.is_absolute():
        path = lexicon_dir / path
    if not path.is_file():
        return []
    payload = _load_yaml_mapping(path)
    raw = payload.get("recommended_kinds", [])
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML file must contain a mapping: {path}")
    return payload


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _lexicon_id(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("id", "name", "lexicon_name"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(item)


def _dedupe(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
