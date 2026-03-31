"""CoDD review — AI-powered artifact quality evaluation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.config import load_project_config
from codd.generator import _invoke_ai_command, _resolve_ai_command
from codd.scanner import _extract_frontmatter


@dataclass
class ReviewIssue:
    """A single issue found during review."""

    severity: str  # CRITICAL, WARNING, INFO
    message: str


@dataclass
class ReviewResult:
    """Result of reviewing a single artifact."""

    node_id: str
    path: str  # relative to project root
    title: str
    verdict: str  # PASS, FAIL
    score: int  # 0-100
    issues: list[ReviewIssue] = field(default_factory=list)
    feedback: str = ""  # detailed feedback for regeneration


@dataclass
class ReviewSummary:
    """Aggregate result of reviewing all artifacts."""

    results: list[ReviewResult]
    pass_count: int = 0
    fail_count: int = 0
    avg_score: float = 0.0


def run_review(
    project_root: Path,
    *,
    scope: str | None = None,
    ai_command: str | None = None,
) -> ReviewSummary:
    """Review design documents for content quality.

    scope: optional node_id filter (e.g. "design:system-design").
           If None, reviews all documents in doc_dirs.
    """
    config = load_project_config(project_root)
    resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="review")

    # Collect documents to review
    docs = _collect_review_targets(project_root, config, scope)
    if not docs:
        return ReviewSummary(results=[])

    # Load upstream context for cross-doc validation
    all_docs = _collect_review_targets(project_root, config, scope=None)
    doc_index = {d["node_id"]: d for d in all_docs}

    results: list[ReviewResult] = []
    for doc in docs:
        result = _review_single_doc(
            project_root, config, doc, doc_index, resolved_ai_command,
        )
        results.append(result)

    pass_count = sum(1 for r in results if r.verdict == "PASS")
    fail_count = len(results) - pass_count
    avg_score = sum(r.score for r in results) / len(results) if results else 0.0

    return ReviewSummary(
        results=results,
        pass_count=pass_count,
        fail_count=fail_count,
        avg_score=avg_score,
    )


def _collect_review_targets(
    project_root: Path,
    config: dict[str, Any],
    scope: str | None,
) -> list[dict[str, Any]]:
    """Collect design documents to review."""
    targets: list[dict[str, Any]] = []
    seen_node_ids: set[str] = set()

    doc_dirs = config.get("scan", {}).get("doc_dirs", [])

    for doc_dir in doc_dirs:
        full_path = project_root / doc_dir
        if not full_path.exists():
            continue

        for md_file in full_path.rglob("*.md"):
            codd_data = _extract_frontmatter(md_file)
            if not codd_data or "node_id" not in codd_data:
                continue

            node_id = codd_data["node_id"]
            if node_id in seen_node_ids:
                continue
            if scope and node_id != scope:
                continue

            seen_node_ids.add(node_id)

            rel_path = md_file.relative_to(project_root).as_posix()
            content = md_file.read_text(encoding="utf-8")

            targets.append({
                "node_id": node_id,
                "path": rel_path,
                "title": codd_data.get("title", node_id),
                "type": codd_data.get("type", "document"),
                "modules": codd_data.get("modules", []),
                "depends_on": codd_data.get("depends_on", []),
                "content": content,
            })

    return targets


def _review_single_doc(
    project_root: Path,
    config: dict[str, Any],
    doc: dict[str, Any],
    doc_index: dict[str, dict[str, Any]],
    ai_command: str,
) -> ReviewResult:
    """Review a single document via AI."""
    # Build upstream context
    upstream_summaries = _build_upstream_context(doc, doc_index)

    prompt = _build_review_prompt(doc, upstream_summaries)
    raw_output = _invoke_ai_command(ai_command, prompt)
    return _parse_review_output(doc["node_id"], doc["path"], doc["title"], raw_output)


def _build_upstream_context(
    doc: dict[str, Any],
    doc_index: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """Build summaries of upstream (depends_on) documents for context."""
    summaries: list[dict[str, str]] = []
    for dep in doc.get("depends_on", []):
        dep_id = dep if isinstance(dep, str) else dep.get("id", "")
        if dep_id in doc_index:
            upstream = doc_index[dep_id]
            # Truncate to first 2000 chars for context
            content = upstream.get("content", "")
            summaries.append({
                "node_id": dep_id,
                "title": upstream.get("title", dep_id),
                "content_preview": content[:2000],
            })
    return summaries


REVIEW_CRITERIA = {
    "requirement": [
        "Completeness: Are all functional and non-functional requirements covered?",
        "Consistency: Are there contradictions between requirements?",
        "Testability: Can each requirement be verified with a concrete test?",
        "Ambiguity: Are requirements specific enough to implement without guessing?",
        "Traceability: Do requirements map to clear system capabilities?",
    ],
    "design": [
        "Architecture soundness: Is the system design technically feasible and well-structured?",
        "Completeness: Does it cover all requirements it claims to implement?",
        "API design: Are interfaces well-defined, consistent, and versioned?",
        "Scalability: Does the design account for growth and load?",
        "Security: Are authentication, authorization, and data protection addressed?",
        "Consistency with upstream: Does it faithfully implement the requirements?",
    ],
    "detailed_design": [
        "Implementation clarity: Can a developer implement this without guessing?",
        "Data model: Are schemas, types, and relationships well-defined?",
        "Error handling: Are failure modes and recovery strategies documented?",
        "Interface contracts: Are function signatures, parameters, and return types clear?",
        "Consistency with system design: Does it align with the parent design doc?",
    ],
    "test": [
        "Coverage: Do tests cover all requirements and acceptance criteria?",
        "Edge cases: Are boundary conditions and error paths tested?",
        "Independence: Are tests isolated and repeatable?",
        "Clarity: Can someone understand what each test verifies?",
        "Traceability: Does each test reference the requirement it verifies?",
    ],
}

DEFAULT_CRITERIA = [
    "Completeness: Does the document cover its stated scope?",
    "Clarity: Is the content clear and actionable?",
    "Consistency: Is it internally consistent and consistent with upstream docs?",
    "Technical accuracy: Are technical details correct and feasible?",
]


def _build_review_prompt(
    doc: dict[str, Any],
    upstream_summaries: list[dict[str, str]],
) -> str:
    """Build a prompt for AI to review an artifact."""
    doc_type = doc.get("type", "document")
    criteria = REVIEW_CRITERIA.get(doc_type, DEFAULT_CRITERIA)

    lines = [
        "You are a SENIOR TECHNICAL REVIEWER evaluating a design artifact.",
        "Your job is to assess the quality of this document and provide structured feedback.",
        "",
        "EVALUATION CRITERIA (score each 0-100, then compute overall weighted average):",
    ]
    for i, criterion in enumerate(criteria, 1):
        lines.append(f"  {i}. {criterion}")

    lines.extend([
        "",
        "OUTPUT FORMAT (strict JSON — no markdown fences, no commentary before/after):",
        '{',
        '  "verdict": "PASS" or "FAIL",',
        '  "score": <integer 0-100>,',
        '  "issues": [',
        '    {"severity": "CRITICAL|WARNING|INFO", "message": "..."},',
        '    ...',
        '  ],',
        '  "feedback": "Detailed feedback for improvement. If PASS, describe strengths. If FAIL, explain exactly what to fix."',
        '}',
        "",
        "SCORING RULES:",
        "- 80-100: PASS — document is production-ready (minor suggestions allowed)",
        "- 60-79: FAIL — significant issues but fixable with targeted edits",
        "- 0-59: FAIL — fundamental problems requiring rewrite",
        "- CRITICAL issues automatically cap score at 59",
        "- Score must be an integer",
        "",
        f"Document: {doc['node_id']}",
        f"Title: {doc['title']}",
        f"Type: {doc_type}",
        f"Modules: {', '.join(doc.get('modules', [])) or 'none'}",
    ])

    if upstream_summaries:
        lines.append("")
        lines.append("--- UPSTREAM DOCUMENTS (for consistency checking) ---")
        for upstream in upstream_summaries:
            lines.append(f"\n### {upstream['node_id']} — {upstream['title']}")
            lines.append(upstream["content_preview"])
        lines.append("--- END UPSTREAM DOCUMENTS ---")

    lines.extend([
        "",
        "--- DOCUMENT UNDER REVIEW ---",
        doc["content"].rstrip(),
        "--- END DOCUMENT ---",
        "",
        "Output the JSON review now. No commentary before or after the JSON.",
    ])

    return "\n".join(lines).rstrip() + "\n"


def _parse_review_output(
    node_id: str,
    path: str,
    title: str,
    raw_output: str,
) -> ReviewResult:
    """Parse AI review output into structured ReviewResult."""
    cleaned = raw_output.strip()

    # Strip markdown fences if present
    fence_match = re.match(r"^\s*```(?:json)?\s*\n(.*)\n```\s*$", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    # Strip any text before the first { or after the last }
    json_start = cleaned.find("{")
    json_end = cleaned.rfind("}")
    if json_start >= 0 and json_end > json_start:
        cleaned = cleaned[json_start:json_end + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # AI didn't return valid JSON — treat as FAIL with feedback
        return ReviewResult(
            node_id=node_id,
            path=path,
            title=title,
            verdict="FAIL",
            score=0,
            issues=[ReviewIssue(severity="CRITICAL", message="Review AI returned invalid JSON")],
            feedback=raw_output[:2000],
        )

    verdict = str(data.get("verdict", "FAIL")).upper()
    if verdict not in ("PASS", "FAIL"):
        verdict = "FAIL"

    score = int(data.get("score", 0))
    score = max(0, min(100, score))

    issues: list[ReviewIssue] = []
    for issue_data in data.get("issues", []):
        severity = str(issue_data.get("severity", "INFO")).upper()
        if severity not in ("CRITICAL", "WARNING", "INFO"):
            severity = "INFO"
        issues.append(ReviewIssue(
            severity=severity,
            message=str(issue_data.get("message", "")),
        ))

    feedback = str(data.get("feedback", ""))

    # Enforce scoring rules
    has_critical = any(i.severity == "CRITICAL" for i in issues)
    if has_critical and score > 59:
        score = 59
        verdict = "FAIL"
    if score < 80:
        verdict = "FAIL"

    return ReviewResult(
        node_id=node_id,
        path=path,
        title=title,
        verdict=verdict,
        score=score,
        issues=issues,
        feedback=feedback,
    )
