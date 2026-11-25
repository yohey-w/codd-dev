"""Run opt-in provisional decisions after the ordinary elicit discovery."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from codd.deployment.providers.ai_command import AiCommandError
from codd.elicit.apply import (
    _approval_states_from_markdown,
    _findings_from_markdown,
)
from codd.elicit.decisions import (
    PROVISIONAL_DECISIONS_PATH,
    ProvisionalDecisionResult,
    _ai_failure_message,
    decision_update_lock,
    filter_decided_findings,
    resolve_provisional_decisions,
)
from codd.elicit.engine import ElicitEngine, _collect_provisional_requirements
from codd.elicit.finding import ElicitResult, Finding
from codd.elicit.formatters.md import MdFormatter
from codd.elicit.persistence import ElicitPersistence


_APPROVED_TODO = re.compile(r"(?m)^- \[[ xX]\] TODO \[([A-Za-z0-9][A-Za-z0-9_.:-]*)\]")
_REVIEW_START = "<!-- codd:elicit-review -->"
_REVIEW_END = "<!-- /codd:elicit-review -->"
_MANAGED_REVIEW = re.compile(
    rf"^{re.escape(_REVIEW_START)}\r?\n(?P<body>.*?)^{re.escape(_REVIEW_END)}\r?$",
    re.MULTILINE | re.DOTALL,
)


def run_provisional_elicit(
    project_root: Path,
    engine: ElicitEngine,
    lexicon_config: Any | None = None,
    *,
    write_markdown: bool = False,
) -> tuple[ElicitResult, ProvisionalDecisionResult]:
    """Resolve discovered and pending gaps, leaving unresolved items in review."""
    persistence = ElicitPersistence(project_root)
    requirements = _collect_provisional_requirements(project_root)
    try:
        discovery = engine.run(project_root, lexicon_config=lexicon_config, requirements_context=requirements)
    except AiCommandError as exc:
        raise ValueError(_ai_failure_message("AI command failed while discovering findings", exc)) from None

    review_path = project_root / "findings.md"
    _, prior_review, approval_states = _read_review(review_path)
    human_ids = _human_review_ids(persistence, approval_states)
    candidates = [finding for finding in discovery.findings if finding.id not in human_ids]
    prior_findings = [
        finding for finding in [*persistence.load_pending(), *prior_review] if finding.id not in human_ids
    ]

    def current_reviewed_ids() -> set[str]:
        return _human_review_ids(persistence, _approval_states_from_markdown(_read_review_text(review_path)))

    decisions = None
    try:
        decisions = resolve_provisional_decisions(
            project_root, candidates, invoke=lambda prompt: engine.invoke(prompt, project_root),
            requirements_context=requirements, reviewed_ids=current_reviewed_ids, prior_findings=prior_findings,
        )
        unresolved = {
            finding_id: _with_unresolved_reason(decisions.unresolved_findings[finding_id], reason)
            for finding_id, reason in decisions.unresolved.items()
        }
        with decision_update_lock(project_root):
            _reconcile_review(persistence, review_path, discovery, decisions, unresolved, write_markdown=write_markdown)
    except OSError as exc:
        report = [str(exc)]
        if decisions is not None:
            report.extend(provisional_detail_lines(decisions))
        if (project_root / PROVISIONAL_DECISIONS_PATH).is_file():
            report.append(f"Provisional decisions: {PROVISIONAL_DECISIONS_PATH.as_posix()}")
        raise OSError("\n".join(report)) from exc

    discovery.metadata["provisional_decisions"] = {
        "path": PROVISIONAL_DECISIONS_PATH.as_posix() if decisions.path.is_file() else None,
        "review_path": review_path.name if decisions.unresolved and review_path.is_file() else None,
        "decided_count": len(decisions.added_ids),
        "already_decided_count": len(decisions.already_decided_ids),
        "unresolved_count": len(decisions.unresolved),
        "decisions": decisions.added_decisions,
        "issues": decisions.issues,
        "unresolved": {
            finding_id: {"question": decisions.unresolved_question(finding_id), "reason": reason}
            for finding_id, reason in decisions.unresolved.items()
        },
    }
    return discovery, decisions


def _reconcile_review(
    persistence: ElicitPersistence,
    review_path: Path,
    discovery: ElicitResult,
    decisions: ProvisionalDecisionResult,
    unresolved: dict[str, Finding],
    *,
    write_markdown: bool,
) -> None:
    """Merge concurrent runs' open findings while holding the decision update lock."""
    review_text, prior_review, approval_states = _read_review(review_path)
    human_ids = _human_review_ids(persistence, approval_states)
    resolved_ids = set(decisions.added_ids) | set(decisions.already_decided_ids)
    open_findings = {
        finding.id: finding
        for finding in [*persistence.load_pending(), *prior_review, *unresolved.values()]
        if finding.id not in human_ids and finding.id not in resolved_ids
    }
    remaining = {
        finding.id: finding
        for finding in filter_decided_findings(persistence.project_root, open_findings.values())
    }
    recently_decided = open_findings.keys() - remaining.keys()
    for finding_id in unresolved.keys() - remaining.keys():
        decisions.unresolved.pop(finding_id)
        decisions.unresolved_findings.pop(finding_id)
        if finding_id in recently_decided:
            decisions.already_decided_ids.append(finding_id)
    resolved_ids.update(recently_decided)
    persistence.reconcile_pending(
        [finding for finding_id, finding in unresolved.items() if finding_id in remaining],
        resolved_ids=resolved_ids - human_ids - remaining.keys(),
    )
    discovery.findings = [finding for finding in prior_review if finding.id in human_ids]
    discovery.findings.extend(remaining.values())
    if discovery.findings:
        discovery.all_covered = False
    if discovery.findings or prior_review or (write_markdown and not review_text):
        markdown = MdFormatter().format(discovery, approval_states=approval_states)
        merged = _merge_review_markdown(review_text, markdown)
        if merged != review_text:
            review_path.write_bytes(merged.encode("utf-8"))


def _read_review(path: Path) -> tuple[str, list[Finding], dict[str, str]]:
    text = _read_review_text(path)
    section = _MANAGED_REVIEW.search(text)
    if section:
        findings = _findings_from_markdown(section["body"])
    else:
        findings = _findings_from_markdown(text) if _is_findings_report(text) else []
    return text, findings, _approval_states_from_markdown(text)


def _read_review_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""


def _is_findings_report(text: str) -> bool:
    normalized = text.replace("\r\n", "\n").removeprefix("\ufeff")
    return normalized.startswith("# Findings\n") and (
        "<!-- codd:finding" in normalized
        or ("\n- id:" in normalized and "\n- kind:" in normalized)
        or normalized == "# Findings\n\nNo findings.\n"
    )


def _merge_review_markdown(text: str, markdown: str) -> str:
    section = _MANAGED_REVIEW.search(text)
    if section:
        start, end = section.span("body")
        return text[:start] + markdown + text[end:]
    if text and not _is_findings_report(text):
        return f"{text}\n\n{_REVIEW_START}\n{markdown}{_REVIEW_END}\n"
    return markdown


def _human_review_ids(persistence: ElicitPersistence, approval_states: dict[str, str]) -> set[str]:
    return set(approval_states) | persistence.load_ignored() | _approved_requirements(persistence.project_root)


def provisional_summary(decisions: ProvisionalDecisionResult, *, include_issues: bool = False) -> str:
    counts = (
        f"provisional decisions={len(decisions.added_ids)}, "
        f"already decided={len(decisions.already_decided_ids)}, unresolved={len(decisions.unresolved)}"
    )
    if decisions.path.is_file():
        counts += f", list={PROVISIONAL_DECISIONS_PATH.as_posix()}"
    if decisions.unresolved:
        counts += ", review=findings.md"
    if include_issues and decisions.issues:
        counts += ", " + ", ".join(f"warning: {_one_line(issue)}" for issue in decisions.issues)
    return counts


def provisional_detail_lines(decisions: ProvisionalDecisionResult) -> list[str]:
    lines = [f"  {finding_id}: {_one_line(choice)}" for finding_id, choice in decisions.added_decisions.items()]
    lines.extend(
        f"  {finding_id} (unresolved): {_one_line(decisions.unresolved_question(finding_id))} — {_one_line(reason)}"
        for finding_id, reason in decisions.unresolved.items()
    )
    lines.extend(f"  Warning: {_one_line(issue)}" for issue in decisions.issues)
    return lines


def _approved_requirements(project_root: Path) -> set[str]:
    """Find explicit approvals previously written by the existing apply command."""
    paths = ("requirements.md", "docs/requirements/requirements.md", "docs/requirements.md", ".codd/requirements.md")
    approved = set()
    for relative in paths:
        path = project_root / relative
        if path.is_file():
            approved.update(_APPROVED_TODO.findall(path.read_text(encoding="utf-8", errors="replace")))
    return approved


def _with_unresolved_reason(finding: Finding, reason: str) -> Finding:
    details = {
        key: value
        for key, value in finding.details.items()
        if key not in {"decision", "approval"}
    }
    details["provisional_decision_reason"] = reason
    return replace(finding, details=details)


def _one_line(text: str) -> str:
    return " ".join(text.split())
