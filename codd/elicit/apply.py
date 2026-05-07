"""Generic apply engine for reviewed elicit findings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

import yaml

from codd.elicit.finding import Finding
from codd.elicit.formatters.md import MdFormatter


@dataclass(frozen=True)
class ApplyResult:
    applied_count: int
    skipped_count: int
    files_updated: list[str]


class ElicitApplyEngine:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.elicit_dir = self.project_root / ".codd" / "elicit"

    def apply(self, approved: list[Finding]) -> ApplyResult:
        self.elicit_dir.mkdir(parents=True, exist_ok=True)
        timestamp = _utc_timestamp()

        ignored_path = self.elicit_dir / "ignored_findings.yaml"
        pending_path = self.elicit_dir / "pending_findings.yaml"
        history_path = self.elicit_dir / "elicit_history.yaml"
        findings_md_path = self.project_root / "findings.md"

        ignored_doc = _read_yaml_mapping(ignored_path, default_key="ignored")
        pending_doc = _read_yaml_mapping(pending_path, default_key="pending")
        history_doc = _read_yaml_mapping(history_path, default_key="sessions")

        ignored_entries = _list_value(ignored_doc, "ignored")
        pending_entries = _list_value(pending_doc, "pending")
        history_entries = _list_value(history_doc, "sessions")

        existing_ids = _existing_finding_ids(ignored_entries) | _existing_finding_ids(pending_entries)
        accepted: list[Finding] = []
        rejected: list[Finding] = []
        deferred: list[Finding] = []
        duplicates: list[Finding] = []

        for finding in approved:
            decision = _finding_decision(finding)
            if decision == "reject":
                rejected.append(finding)
                continue
            if decision == "defer":
                deferred.append(finding)
                continue
            if finding.id in existing_ids:
                duplicates.append(finding)
                continue
            accepted.append(finding)
            existing_ids.add(finding.id)

        for finding in rejected:
            ignored_entries.append(_ignored_entry(finding, timestamp))
        for finding in accepted:
            pending_entries.append(_pending_entry(finding, timestamp))

        history_entries.append(
            {
                "timestamp": timestamp,
                "findings_total": len(approved),
                "approved": len(accepted),
                "rejected": len(rejected),
                "deferred": len(deferred),
                "duplicates": len(duplicates),
                "findings_md": "findings.md" if accepted else None,
            }
        )

        files_updated: list[str] = []
        _write_yaml(ignored_path, {"ignored": ignored_entries})
        files_updated.append(_relative_path(ignored_path, self.project_root))
        _write_yaml(pending_path, {"pending": pending_entries})
        files_updated.append(_relative_path(pending_path, self.project_root))
        _write_yaml(history_path, {"sessions": history_entries})
        files_updated.append(_relative_path(history_path, self.project_root))

        if accepted:
            findings_md_path.write_text(MdFormatter().format(accepted), encoding="utf-8")
            files_updated.append(_relative_path(findings_md_path, self.project_root))

        return ApplyResult(
            applied_count=len(accepted),
            skipped_count=len(rejected) + len(deferred) + len(duplicates),
            files_updated=files_updated,
        )


def load_findings_from_file(input_file: Path, format_name: str | None = None) -> list[Finding]:
    path = Path(input_file)
    raw = path.read_text(encoding="utf-8")
    resolved_format = format_name or _detect_format(path)
    if resolved_format == "json":
        return _findings_from_json(raw)
    if resolved_format == "md":
        return _findings_from_markdown(raw)
    raise ValueError(f"Unsupported elicit input format: {resolved_format}")


def _detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in {".md", ".markdown"}:
        return "md"
    raise ValueError("Could not detect elicit input format; pass --format md or --format json")


def _findings_from_json(raw: str) -> list[Finding]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("Finding JSON input must be an array")
    return [Finding.from_dict(item) for item in payload]


_FINDING_COMMENT_RE = re.compile(r"<!--\s*codd:finding\s*(.*?)\s*-->", re.DOTALL)


def _findings_from_markdown(raw: str) -> list[Finding]:
    findings = [Finding.from_dict(json.loads(match.group(1))) for match in _FINDING_COMMENT_RE.finditer(raw)]
    if findings:
        return findings
    return _findings_from_markdown_fields(raw)


def _findings_from_markdown_fields(raw: str) -> list[Finding]:
    findings: list[Finding] = []
    for section in re.split(r"(?m)^##\s+", raw)[1:]:
        fields: dict[str, Any] = {}
        for key in ("id", "kind", "severity", "name", "question", "rationale"):
            match = re.search(rf"(?m)^-\s+{key}:\s*(?:`([^`]*)`|(.+))\s*$", section)
            if match:
                value = (match.group(1) if match.group(1) is not None else match.group(2)).strip()
                if value != "N/A":
                    fields[key] = value
        if fields:
            findings.append(Finding.from_dict(fields))
    return findings


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _existing_finding_ids(entries: list[Any]) -> set[str]:
    ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "id" in entry:
            ids.add(str(entry["id"]))
            continue
        finding = entry.get("finding")
        if isinstance(finding, dict) and "id" in finding:
            ids.add(str(finding["id"]))
    return ids


def _finding_decision(finding: Finding) -> str:
    raw = finding.details.get("decision", finding.details.get("approval", "approve"))
    value = str(raw).strip().lower()
    if value in {"reject", "rejected", "no", "n"}:
        return "reject"
    if value in {"defer", "deferred", "later", "pending", "d"}:
        return "defer"
    return "approve"


def _ignored_entry(finding: Finding, timestamp: str) -> dict[str, Any]:
    return {
        "id": finding.id,
        "kind": finding.kind,
        "ignored_at": timestamp,
        "reason": str(finding.details.get("reason") or finding.details.get("decision") or "rejected"),
        "finding": finding.to_dict(),
    }


def _pending_entry(finding: Finding, timestamp: str) -> dict[str, Any]:
    return {
        "finding": finding.to_dict(),
        "discovered_at": timestamp,
        "last_review_at": None,
    }


def _relative_path(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()
