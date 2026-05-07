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
        requirements_path = _requirements_path(self.project_root)

        ignored_doc = _read_yaml_mapping(ignored_path, default_key="ignored")
        pending_doc = _read_yaml_mapping(pending_path, default_key="pending")
        history_doc = _read_yaml_mapping(history_path, default_key="sessions")

        ignored_entries = _list_value(ignored_doc, "ignored")
        pending_entries = _list_value(pending_doc, "pending")
        history_entries = _list_value(history_doc, "sessions")

        ignored_ids = _existing_finding_ids(ignored_entries)
        pending_ids = _existing_finding_ids(pending_entries)
        requirements_ids = _existing_ids_in_file(requirements_path)
        accepted_for_requirements: list[Finding] = []
        accepted_for_review: list[Finding] = []
        pending_review: list[Finding] = []
        rejected: list[Finding] = []
        deferred: list[Finding] = []
        duplicates: list[Finding] = []

        for finding in approved:
            decision = _finding_decision(finding)
            if decision == "reject":
                _remove_finding_entry(pending_entries, finding.id)
                pending_ids.discard(finding.id)
                if finding.id in ignored_ids:
                    duplicates.append(finding)
                    continue
                rejected.append(finding)
                ignored_ids.add(finding.id)
                continue
            if decision == "defer":
                deferred.append(finding)
                continue
            if decision == "pending":
                if finding.id in ignored_ids or finding.id in requirements_ids:
                    duplicates.append(finding)
                    continue
                if finding.id in pending_ids:
                    duplicates.append(finding)
                    continue
                pending_review.append(finding)
                pending_ids.add(finding.id)
                continue
            if _writes_requirements(finding):
                if finding.id in ignored_ids or finding.id in requirements_ids:
                    duplicates.append(finding)
                    continue
                _remove_finding_entry(pending_entries, finding.id)
                pending_ids.discard(finding.id)
                accepted_for_requirements.append(finding)
                requirements_ids.add(finding.id)
                continue
            if finding.id in ignored_ids or finding.id in pending_ids or finding.id in requirements_ids:
                duplicates.append(finding)
                continue
            accepted_for_review.append(finding)
            pending_ids.add(finding.id)

        for finding in rejected:
            ignored_entries.append(_ignored_entry(finding, timestamp))
        for finding in [*accepted_for_review, *pending_review]:
            pending_entries.append(_pending_entry(finding, timestamp))

        history_entries.append(
            {
                "timestamp": timestamp,
                "findings_total": len(approved),
                "approved": len(accepted_for_requirements) + len(accepted_for_review),
                "rejected": len(rejected),
                "deferred": len(deferred),
                "pending": len(pending_review),
                "duplicates": len(duplicates),
                "findings_md": "findings.md" if accepted_for_review else None,
                "requirements_md": _relative_path(requirements_path, self.project_root)
                if accepted_for_requirements
                else None,
            }
        )

        files_updated: list[str] = []
        _write_yaml(ignored_path, {"ignored": ignored_entries})
        files_updated.append(_relative_path(ignored_path, self.project_root))
        _write_yaml(pending_path, {"pending": pending_entries})
        files_updated.append(_relative_path(pending_path, self.project_root))
        _write_yaml(history_path, {"sessions": history_entries})
        files_updated.append(_relative_path(history_path, self.project_root))

        if accepted_for_requirements:
            _append_requirements(requirements_path, accepted_for_requirements)
            files_updated.append(_relative_path(requirements_path, self.project_root))

        if accepted_for_review:
            findings_md_path.write_text(MdFormatter().format(accepted_for_review), encoding="utf-8")
            files_updated.append(_relative_path(findings_md_path, self.project_root))

        return ApplyResult(
            applied_count=len(accepted_for_requirements) + len(accepted_for_review) + len(pending_review),
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
_APPROVAL_LINE_RE = re.compile(
    r"(?m)^-\s+approval:\s*\[([^\]]*)\]\s*`?([A-Za-z0-9][A-Za-z0-9_.:-]*)`?"
)


def _findings_from_markdown(raw: str) -> list[Finding]:
    findings = [Finding.from_dict(json.loads(match.group(1))) for match in _FINDING_COMMENT_RE.finditer(raw)]
    if findings:
        approval_states = _approval_states_from_markdown(raw)
        return [_with_approval_state(finding, approval_states.get(finding.id)) for finding in findings]
    return _findings_from_markdown_fields(raw)


def _findings_from_markdown_fields(raw: str) -> list[Finding]:
    findings: list[Finding] = []
    approval_states = _approval_states_from_markdown(raw)
    for section in re.split(r"(?m)^##\s+", raw)[1:]:
        fields: dict[str, Any] = {}
        for key in ("id", "kind", "severity", "name", "question", "rationale"):
            match = re.search(rf"(?m)^-\s+{key}:\s*(?:`([^`]*)`|(.+))\s*$", section)
            if match:
                value = (match.group(1) if match.group(1) is not None else match.group(2)).strip()
                if value != "N/A":
                    fields[key] = value
        if fields:
            finding = Finding.from_dict(fields)
            findings.append(_with_approval_state(finding, approval_states.get(finding.id)))
    return findings


def _approval_states_from_markdown(raw: str) -> dict[str, str]:
    states: dict[str, str] = {}
    for match in _APPROVAL_LINE_RE.finditer(raw):
        state = match.group(1).strip().lower()
        finding_id = match.group(2).strip().strip("`")
        if state:
            states[finding_id] = state
    return states


def _with_approval_state(finding: Finding, state: str | None) -> Finding:
    if state is None:
        return finding
    details = dict(finding.details)
    if state in {"x", "y", "yes", "approve", "approved"}:
        details["approval"] = "approved"
    elif state in {"r", "n", "no", "reject", "rejected"}:
        details["approval"] = "rejected"
    else:
        details["approval"] = f"unknown:{state}"
    payload = finding.to_dict()
    payload["details"] = details
    return Finding.from_dict(payload)


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


def _remove_finding_entry(entries: list[Any], finding_id: str) -> None:
    entries[:] = [
        entry
        for entry in entries
        if _entry_finding_id(entry) != finding_id
    ]


def _entry_finding_id(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    if "id" in entry:
        return str(entry["id"])
    finding = entry.get("finding")
    if isinstance(finding, dict) and "id" in finding:
        return str(finding["id"])
    return None


def _finding_decision(finding: Finding) -> str:
    raw = finding.details.get("decision", finding.details.get("approval", "approve"))
    value = str(raw).strip().lower()
    if value.startswith("unknown:"):
        return "pending"
    if value in {"reject", "rejected", "no", "n", "r"}:
        return "reject"
    if value in {"pending", "unchecked", "unreviewed"}:
        return "pending"
    if value in {"defer", "deferred", "later", "d"}:
        return "defer"
    return "approve"


def _writes_requirements(finding: Finding) -> bool:
    raw = finding.details.get("decision", finding.details.get("approval"))
    if raw is None:
        return False
    return str(raw).strip().lower() in {"approve", "approved", "yes", "y", "x"}


def _requirements_path(project_root: Path) -> Path:
    candidates = [
        project_root / "requirements.md",
        project_root / "docs" / "requirements" / "requirements.md",
        project_root / "docs" / "requirements.md",
        project_root / ".codd" / "requirements.md",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _existing_ids_in_file(path: Path) -> set[str]:
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"\[([A-Za-z0-9][A-Za-z0-9_.:-]*)\]", text))


def _append_requirements(path: Path, findings: list[Finding]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    entries = "\n\n".join(_requirements_entry(finding) for finding in findings)
    parts = [existing.rstrip()]
    heading = "## TODO (codd elicit approved findings)"
    if heading not in existing:
        parts.extend(["", heading])
    parts.extend(["", entries])
    path.write_text("\n".join(part for part in parts if part != "").rstrip() + "\n", encoding="utf-8")


def _requirements_entry(finding: Finding) -> str:
    lines = [f"- [ ] TODO [{finding.id}] {_summary(finding)}"]
    if finding.question:
        lines.append(f"  - Question: {finding.question}")
    if finding.rationale:
        lines.append(f"  - Rationale: {finding.rationale}")
    if finding.related_requirement_ids:
        lines.append(f"  - Related requirements: {', '.join(finding.related_requirement_ids)}")
    return "\n".join(lines)


def _summary(finding: Finding) -> str:
    return finding.name or finding.question or finding.rationale or finding.id


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
