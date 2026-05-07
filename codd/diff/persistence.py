"""YAML persistence for comparison discovery state."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


class DiffPersistence:
    """Read and write comparison state files under ``.codd/diff``."""

    def __init__(self, project_root: Path | str):
        self.project_root = Path(project_root)
        self.base_dir = self.project_root / ".codd" / "diff"
        self.ignored_path = self.base_dir / "ignored_findings.yaml"
        self.history_path = self.base_dir / "diff_history.yaml"

    def load_ignored(self) -> set[str]:
        payload = _read_yaml_mapping(self.ignored_path, default_key="ignored")
        entries = _list_value(payload, "ignored")
        ignored: set[str] = set()
        for entry in entries:
            finding_id = _finding_id_from_entry(entry)
            if finding_id:
                ignored.add(finding_id)
        return ignored

    def save_ignored(self, finding_id: str, reason: str) -> None:
        payload = _read_yaml_mapping(self.ignored_path, default_key="ignored")
        entries = _list_value(payload, "ignored")
        if finding_id not in {_finding_id_from_entry(entry) for entry in entries}:
            entries.append({"id": finding_id, "ignored_at": _utc_timestamp(), "reason": reason})
        _write_yaml(self.ignored_path, {"ignored": entries})

    def append_history(self, session_record: dict[str, Any]) -> None:
        payload = _read_yaml_mapping(self.history_path, default_key="sessions")
        sessions = _list_value(payload, "sessions")
        sessions.append(dict(session_record))
        _write_yaml(self.history_path, {"sessions": sessions})


def load_ignored(project_root: Path | str) -> set[str]:
    return DiffPersistence(project_root).load_ignored()


def save_ignored(project_root: Path | str, finding_id: str, reason: str) -> None:
    DiffPersistence(project_root).save_ignored(finding_id, reason)


def append_history(project_root: Path | str, session_record: dict[str, Any]) -> None:
    DiffPersistence(project_root).append_history(session_record)


def _read_yaml_mapping(path: Path, *, default_key: str) -> dict[str, Any]:
    if not path.exists():
        return {default_key: []}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if payload is None:
        return {default_key: []}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    payload.setdefault(default_key, [])
    return payload


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _list_value(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if value is None:
        value = []
        payload[key] = value
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a YAML list")
    return value


def _finding_id_from_entry(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return None
    if "id" in entry:
        return str(entry["id"])
    finding = entry.get("finding")
    if isinstance(finding, dict) and "id" in finding:
        return str(finding["id"])
    return None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["DiffPersistence", "append_history", "load_ignored", "save_ignored"]
