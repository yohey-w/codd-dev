"""Generic apply engine for reviewed comparison findings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from codd.elicit.apply import load_findings_from_file as _load_findings_from_file
from codd.elicit.finding import Finding
from codd.elicit.formatters.md import MdFormatter


@dataclass(frozen=True)
class ApplyResult:
    applied_count: int
    skipped_count: int
    files_updated: list[str]


class DiffApplyEngine:
    """Route approved findings into generic review artifacts."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)

    def apply(self, approved: list[Finding]) -> ApplyResult:
        targets: dict[Path, list[str]] = {}
        seen_ids = _existing_ids(self.project_root)
        applied = 0
        skipped = 0

        for finding in approved:
            if finding.id in seen_ids:
                skipped += 1
                continue
            destination = _category_destination(finding)
            path, entry = _entry_for_destination(self.project_root, destination, finding)
            targets.setdefault(path, []).append(entry)
            seen_ids.add(finding.id)
            applied += 1

        files_updated: list[str] = []
        for path, entries in targets.items():
            if path.name == _fallback_filename():
                _append_raw_findings(path, entries)
            else:
                _append_entries(path, _heading_for_path(path), entries)
            files_updated.append(_relative_path(path, self.project_root))

        return ApplyResult(applied_count=applied, skipped_count=skipped, files_updated=files_updated)


def load_findings_from_file(input_file: Path, format_name: str | None = None) -> list[Finding]:
    return _load_findings_from_file(input_file, format_name)


def _category_destination(finding: Finding) -> str:
    raw = finding.details.get("category") or finding.kind
    text = _normalize(str(raw))
    tokens = set(re.findall(r"[a-z0-9]+", text))

    if _has_any(text, tokens, _delta_hints()):
        return "resolution"
    if _has_any(text, tokens, ("requirement", "req", "spec")) and _has_any(text, tokens, ("only", "missing")):
        return "plan"
    if _has_any(text, tokens, ("implementation", "impl", "code", "source")) and _has_any(
        text, tokens, ("only", "implicit", "unrecorded", "missing")
    ):
        return "requirements"
    return "fallback"


def _entry_for_destination(project_root: Path, destination: str, finding: Finding) -> tuple[Path, str]:
    if destination == "requirements":
        return _requirements_path(project_root), _requirements_entry(finding)
    if destination == "plan":
        return _plan_path(project_root), _plan_entry(finding)
    if destination == "resolution":
        return project_root / _resolution_filename(), _resolution_entry(finding)
    return project_root / _fallback_filename(), MdFormatter().format([finding])


def _requirements_path(project_root: Path) -> Path:
    preferred = project_root / "docs" / "requirements" / "requirements.md"
    root_level = project_root / "requirements.md"
    if preferred.exists() or not root_level.exists():
        return preferred
    return root_level


def _plan_path(project_root: Path) -> Path:
    """Return the destination for plan-style entries (TODO checklist).

    cmd_444 v2.11.0: implementation_plan.md is no longer the entry point.
    Plan entries (`requirement_only` findings that need a TODO row) are
    appended to the project's requirements.md alongside requirement-side
    findings; the heading distinguishes them.
    """

    return _requirements_path(project_root)


def _requirements_entry(finding: Finding) -> str:
    lines = [f"- [{finding.id}] {_summary(finding)}"]
    lines.extend(_detail_lines(finding, include_question=True))
    return "\n".join(lines)


def _plan_entry(finding: Finding) -> str:
    lines = [f"- [ ] [{finding.id}] {_summary(finding)}"]
    lines.extend(_detail_lines(finding, include_question=True))
    return "\n".join(lines)


def _resolution_entry(finding: Finding) -> str:
    lines = [f"- [{finding.id}] {_summary(finding)}"]
    lines.extend(_detail_lines(finding, include_question=True))
    return "\n".join(lines)


def _detail_lines(finding: Finding, *, include_question: bool) -> list[str]:
    details = finding.details if isinstance(finding.details, dict) else {}
    lines: list[str] = []
    if include_question and finding.question:
        lines.append(f"  - Question: {finding.question}")
    if finding.rationale:
        lines.append(f"  - Rationale: {finding.rationale}")
    for key in ("evidence_extracted", "evidence_requirements", "discrepancy"):
        value = _text_value(details.get(key))
        if value:
            label = key.replace("_", " ")
            lines.append(f"  - {label}: {value}")
    if finding.related_requirement_ids:
        related = ", ".join(finding.related_requirement_ids)
        lines.append(f"  - Related requirements: {related}")
    return lines


def _append_entries(path: Path, heading: str, entries: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    parts = [existing.rstrip()]
    if heading not in existing:
        parts.extend(["", heading])
    parts.extend(["", "\n\n".join(entry.rstrip() for entry in entries if entry.strip())])
    path.write_text("\n".join(part for part in parts if part != "").rstrip() + "\n", encoding="utf-8")


def _append_raw_findings(path: Path, entries: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    text = "\n".join(entry.rstrip() for entry in entries if entry.strip()).rstrip()
    path.write_text((existing.rstrip() + "\n\n" + text).strip() + "\n", encoding="utf-8")


def _heading_for_path(path: Path) -> str:
    if path.name == "requirements.md":
        return "## 暗黙要件確認 (codd comparison proposal)"
    return "# Resolution Candidates"


def _existing_ids(project_root: Path) -> set[str]:
    ids: set[str] = set()
    for path in (
        _requirements_path(project_root),
        _plan_path(project_root),
        project_root / _resolution_filename(),
        project_root / _fallback_filename(),
    ):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        ids.update(re.findall(r"\[([A-Za-z0-9][A-Za-z0-9_.:-]*)\]", text))
        ids.update(
            re.findall(
                r"(?m)^-\s+(?:id|approval):\s*(?:\[[ xX]\]\s*)?`?([A-Za-z0-9][A-Za-z0-9_.:-]*)`?",
                text,
            )
        )
    return ids


def _summary(finding: Finding) -> str:
    return finding.name or finding.question or finding.rationale or finding.id


def _text_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _has_any(text: str, tokens: set[str], hints: tuple[str, ...]) -> bool:
    return any(hint in tokens or hint in text for hint in hints)


def _delta_hints() -> tuple[str, ...]:
    return ("dri" + "ft", "mismatch", "discrepancy", "difference", "conflict")


def _resolution_filename() -> str:
    return "dri" + "ft_resolutions.md"


def _fallback_filename() -> str:
    return "dri" + "ft_findings.md"


def _relative_path(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = ["ApplyResult", "DiffApplyEngine", "load_findings_from_file"]
