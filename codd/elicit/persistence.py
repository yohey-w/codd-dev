"""YAML persistence for elicit discovery state."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from codd.elicit.finding import Finding


class ElicitPersistence:
    """Read and write elicit state files under ``.codd/elicit``."""

    def __init__(self, project_root: Path | str):
        self.project_root = Path(project_root)
        self.elicit_dir = self.project_root / ".codd" / "elicit"
        self.ignored_path = self.elicit_dir / "ignored_findings.yaml"
        self.pending_path = self.elicit_dir / "pending_findings.yaml"
        self.history_path = self.elicit_dir / "elicit_history.yaml"

    def load_ignored(self) -> set[str]:
        payload = _read_yaml_mapping(self.ignored_path, default_key="ignored")
        entries = _list_value(payload, "ignored")
        ignored: set[str] = set()
        for entry in entries:
            finding_id = _finding_id_from_entry(entry)
            if finding_id:
                ignored.add(finding_id)
        return ignored

    def load_pending(self) -> list[Finding]:
        payload = _read_yaml_mapping(self.pending_path, default_key="pending")
        entries = _list_value(payload, "pending")
        findings: list[Finding] = []
        for entry in entries:
            finding_payload = _finding_payload_from_entry(entry)
            if finding_payload is None:
                continue
            findings.append(Finding.from_dict(finding_payload))
        return findings

    def save_pending(self, findings: list[Finding]) -> None:
        timestamp = _utc_timestamp()
        entries = [
            {
                "finding": finding.to_dict(),
                "discovered_at": timestamp,
                "last_review_at": None,
            }
            for finding in findings
        ]
        _write_yaml(self.pending_path, {"pending": entries})

    def append_history(self, session_record: dict[str, Any]) -> None:
        payload = _read_yaml_mapping(self.history_path, default_key="sessions")
        sessions = _list_value(payload, "sessions")
        sessions.append(dict(session_record))
        _write_yaml(self.history_path, {"sessions": sessions})

    def filter_known(self, findings: list[Finding]) -> list[Finding]:
        known_ids = self.load_ignored() | {finding.id for finding in self.load_pending()}
        return [finding for finding in findings if finding.id not in known_ids]


def load_ignored(project_root: Path | str) -> set[str]:
    return ElicitPersistence(project_root).load_ignored()


def load_pending(project_root: Path | str) -> list[Finding]:
    return ElicitPersistence(project_root).load_pending()


def save_pending(project_root: Path | str, findings: list[Finding]) -> None:
    ElicitPersistence(project_root).save_pending(findings)


def append_history(project_root: Path | str, session_record: dict[str, Any]) -> None:
    ElicitPersistence(project_root).append_history(session_record)


def filter_known_findings(project_root: Path | str, findings: list[Finding]) -> list[Finding]:
    return ElicitPersistence(project_root).filter_known(findings)


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
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


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


def _finding_payload_from_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    finding = entry.get("finding")
    if isinstance(finding, dict):
        return finding
    if {"id", "kind", "severity"}.issubset(entry):
        return entry
    return None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ElicitPersistence",
    "append_history",
    "filter_known_findings",
    "load_ignored",
    "load_pending",
    "save_pending",
]
