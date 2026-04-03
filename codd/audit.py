"""CoDD audit — consolidated change review pack for stakeholders.

Bundles validate + impact + review into a single report that PM/QA/部長
can use to make merge/release decisions.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from codd.config import find_codd_dir, load_project_config
from codd.graph import CEG
from codd.propagate import (
    _check_conventions_from_graph,
    _get_changed_files,
    _resolve_start_nodes,
)
from codd.reviewer import ReviewSummary, run_review
from codd.validator import ValidationResult, validate_project


@dataclass
class AuditResult:
    """Consolidated change review pack."""

    timestamp: str
    diff_target: str
    changed_files: list[str]
    validation: ValidationResult
    impact_nodes: dict[str, dict]
    convention_alerts: list[dict]
    review: ReviewSummary | None
    verdict: str  # APPROVE, CONDITIONAL, REJECT

    @property
    def risk_level(self) -> str:
        if self.verdict == "REJECT":
            return "HIGH"
        if self.verdict == "CONDITIONAL":
            return "MEDIUM"
        return "LOW"


def run_audit(
    project_root: Path,
    diff_target: str = "HEAD",
    *,
    ai_command: str | None = None,
    skip_review: bool = False,
) -> AuditResult:
    """Run the full audit pipeline: validate → impact → review."""
    project_root = project_root.resolve()
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        raise FileNotFoundError("CoDD config dir not found. Run 'codd init' first.")

    config = load_project_config(project_root)

    # Phase 1: Validate document integrity
    validation = validate_project(project_root, codd_dir)

    # Phase 2: Impact analysis
    changed_files = _get_changed_files(project_root, diff_target)
    impact_nodes: dict[str, dict] = {}
    convention_alerts: list[dict] = []

    if changed_files:
        scan_dir = codd_dir / "scan"
        if scan_dir.exists():
            bands = config.get("bands", {})
            max_depth = config.get("propagation", {}).get("max_depth", 10)

            ceg = CEG(scan_dir)
            try:
                start_nodes = _resolve_start_nodes(ceg, project_root, changed_files)

                for node_id, source_file in start_nodes:
                    impacts = ceg.propagate_impact(node_id, max_depth=max_depth)
                    for target_id, info in impacts.items():
                        if target_id not in impact_nodes or info["depth"] < impact_nodes[target_id]["depth"]:
                            impact_nodes[target_id] = {
                                **info,
                                "source": source_file,
                            }

                raw_conventions = _check_conventions_from_graph(ceg, start_nodes)
                for conv in raw_conventions:
                    convention_alerts.append({
                        "source": conv.get("source", ""),
                        "target": conv.get("target", ""),
                        "rule": conv.get("convention", conv.get("rule", "")),
                    })
            finally:
                ceg.close()

    # Phase 3: AI review (optional — expensive)
    review_summary: ReviewSummary | None = None
    if not skip_review:
        try:
            review_summary = run_review(project_root, ai_command=ai_command)
        except (FileNotFoundError, ValueError):
            pass  # No docs to review is OK

    # Verdict
    verdict = _determine_verdict(validation, impact_nodes, convention_alerts, review_summary)

    return AuditResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        diff_target=diff_target,
        changed_files=changed_files,
        validation=validation,
        impact_nodes=impact_nodes,
        convention_alerts=convention_alerts,
        review=review_summary,
        verdict=verdict,
    )


def _determine_verdict(
    validation: ValidationResult,
    impact_nodes: dict[str, dict],
    convention_alerts: list[dict],
    review: ReviewSummary | None,
) -> str:
    """Determine overall verdict: APPROVE, CONDITIONAL, REJECT."""
    # Hard reject: validation errors
    if validation.error_count > 0:
        return "REJECT"

    # Hard reject: review failures with critical issues
    if review and review.fail_count > 0:
        has_critical = any(
            issue.severity == "CRITICAL"
            for result in review.results
            for issue in result.issues
        )
        if has_critical:
            return "REJECT"

    # Conditional: convention alerts or review warnings
    if convention_alerts:
        return "CONDITIONAL"
    if review and review.fail_count > 0:
        return "CONDITIONAL"
    if validation.warning_count > 0:
        return "CONDITIONAL"

    return "APPROVE"


def format_audit_text(result: AuditResult) -> str:
    """Format audit result as human-readable text report."""
    lines: list[str] = []

    # Header
    lines.append("=" * 60)
    lines.append("  CoDD Change Review Pack")
    lines.append(f"  {result.timestamp}")
    lines.append(f"  Diff: {result.diff_target}")
    lines.append("=" * 60)
    lines.append("")

    # Verdict banner
    icon = {"APPROVE": "✅", "CONDITIONAL": "⚠️", "REJECT": "❌"}[result.verdict]
    lines.append(f"  VERDICT: {icon} {result.verdict} (Risk: {result.risk_level})")
    lines.append("")

    # Section 1: Changed files
    lines.append(f"── Changed Files ({len(result.changed_files)}) ──")
    if result.changed_files:
        for f in result.changed_files:
            lines.append(f"  {f}")
    else:
        lines.append("  (no changes detected)")
    lines.append("")

    # Section 2: Validation
    v = result.validation
    v_status = "PASS" if v.error_count == 0 else "FAIL"
    lines.append(f"── Validation: {v_status} ──")
    lines.append(f"  Docs checked: {v.documents_checked}")
    lines.append(f"  Errors: {v.error_count}  Warnings: {v.warning_count}")
    if v.error_count > 0 or v.warning_count > 0:
        for issue in v.issues:
            lines.append(f"  [{issue.level}] {issue.location}: {issue.message}")
    lines.append("")

    # Section 3: Impact
    lines.append(f"── Impact Analysis ({len(result.impact_nodes)} affected nodes) ──")
    if result.impact_nodes:
        for node_id, info in sorted(result.impact_nodes.items(), key=lambda x: x[1].get("depth", 0)):
            depth = info.get("depth", "?")
            conf = info.get("confidence", 0)
            source = info.get("source", "")
            lines.append(f"  {node_id}  depth={depth}  conf={conf:.2f}  from={source}")
    else:
        lines.append("  (no impact graph data — run 'codd scan' first)")
    lines.append("")

    # Section 4: Convention alerts
    if result.convention_alerts:
        lines.append(f"── Convention Alerts ({len(result.convention_alerts)}) ──")
        for alert in result.convention_alerts:
            lines.append(f"  ⚠ {alert['source']} → {alert['target']}: {alert['rule']}")
        lines.append("")

    # Section 5: Review
    if result.review:
        r = result.review
        r_status = "PASS" if r.fail_count == 0 else f"{r.fail_count} FAIL"
        lines.append(f"── Quality Review: {r_status} (avg score: {r.avg_score:.0f}) ──")
        for res in result.review.results:
            icon = "✓" if res.verdict == "PASS" else "✗"
            lines.append(f"  {icon} {res.path} ({res.node_id}) — {res.score}/100")
            for issue in res.issues:
                lines.append(f"    [{issue.severity}] {issue.message}")
        lines.append("")
    else:
        lines.append("── Quality Review: SKIPPED ──")
        lines.append("")

    # Footer
    lines.append("=" * 60)
    lines.append(f"  Action Required: {_action_required(result)}")
    lines.append("=" * 60)

    return "\n".join(lines)


def format_audit_json(result: AuditResult) -> str:
    """Format audit result as JSON."""
    data = {
        "timestamp": result.timestamp,
        "diff_target": result.diff_target,
        "verdict": result.verdict,
        "risk_level": result.risk_level,
        "changed_files": result.changed_files,
        "validation": {
            "status": "PASS" if result.validation.error_count == 0 else "FAIL",
            "documents_checked": result.validation.documents_checked,
            "errors": result.validation.error_count,
            "warnings": result.validation.warning_count,
            "issues": [
                {"level": i.level, "code": i.code, "location": i.location, "message": i.message}
                for i in result.validation.issues
            ],
        },
        "impact": {
            "affected_nodes": len(result.impact_nodes),
            "nodes": {
                node_id: {
                    "depth": info.get("depth", 0),
                    "confidence": info.get("confidence", 0),
                    "source": info.get("source", ""),
                }
                for node_id, info in result.impact_nodes.items()
            },
        },
        "convention_alerts": result.convention_alerts,
        "review": None,
        "action_required": _action_required(result),
    }

    if result.review:
        data["review"] = {
            "pass_count": result.review.pass_count,
            "fail_count": result.review.fail_count,
            "avg_score": round(result.review.avg_score, 1),
            "results": [
                {
                    "node_id": r.node_id,
                    "path": r.path,
                    "verdict": r.verdict,
                    "score": r.score,
                    "issues": [{"severity": i.severity, "message": i.message} for i in r.issues],
                }
                for r in result.review.results
            ],
        }

    return json.dumps(data, ensure_ascii=False, indent=2)


def _action_required(result: AuditResult) -> str:
    """One-line action summary for the reviewer."""
    if result.verdict == "APPROVE":
        return "Safe to merge. No issues found."
    if result.verdict == "REJECT":
        reasons = []
        if result.validation.error_count > 0:
            reasons.append(f"{result.validation.error_count} validation error(s)")
        if result.review and any(
            i.severity == "CRITICAL" for r in result.review.results for i in r.issues
        ):
            reasons.append("critical review issue(s)")
        return f"Fix before merge: {'; '.join(reasons)}."
    # CONDITIONAL
    reasons = []
    if result.convention_alerts:
        reasons.append(f"{len(result.convention_alerts)} convention alert(s)")
    if result.review and result.review.fail_count > 0:
        reasons.append(f"{result.review.fail_count} review failure(s)")
    if result.validation.warning_count > 0:
        reasons.append(f"{result.validation.warning_count} validation warning(s)")
    return f"Review needed: {'; '.join(reasons)}."
