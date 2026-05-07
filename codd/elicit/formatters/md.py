"""Markdown formatter for elicit findings."""

from __future__ import annotations

import json
import re

import yaml

from codd.elicit.finding import ElicitResult, Finding


_CHECKED_APPROVAL_RE = re.compile(r"^\s*-\s*approval:\s*\[[xX]\]\s*`?([^`\s]+)`?", re.MULTILINE)


class MdFormatter:
    name = "md"

    def format(self, findings: list[Finding] | ElicitResult) -> str:
        result = _coerce_result(findings)
        coverage_section = _coverage_section(result)
        if not result.findings:
            if result.all_covered:
                body = "✅ All lexicon categories covered. No action required.\n"
            else:
                body = "No findings.\n"
            return f"# Findings\n\n{coverage_section}{body}"

        lines = ["# Findings", ""]
        if coverage_section:
            lines.extend(coverage_section.rstrip("\n").split("\n"))
            lines.append("")
        for finding in result.findings:
            lines.extend(_format_finding(finding))
        return "\n".join(lines).rstrip() + "\n"

    def parse_approval(self, raw: str) -> list[str]:
        stripped = raw.strip()
        if not stripped:
            return []

        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list) and all(isinstance(item, str) for item in payload):
            return list(payload)

        approved = _CHECKED_APPROVAL_RE.findall(raw)
        if approved:
            return approved

        tokens = [token.strip().strip("`") for token in re.split(r"[\s,]+", stripped) if token.strip()]
        return [token for token in tokens if token]


def _format_finding(finding: Finding) -> list[str]:
    name = finding.name or finding.id
    payload = json.dumps(finding.to_dict(), ensure_ascii=False, sort_keys=True)
    lines = [
        "<!-- codd:finding",
        payload,
        "-->",
        f"## {finding.id} - {name}",
        "",
        f"- approval: [ ] `{finding.id}`",
        f"- id: `{finding.id}`",
        f"- kind: `{finding.kind}`",
        f"- severity: `{finding.severity}`",
        f"- name: {_display_value(finding.name)}",
        f"- question: {_display_value(finding.question)}",
        f"- rationale: {_display_value(finding.rationale)}",
    ]
    if finding.related_requirement_ids:
        related = ", ".join(f"`{item}`" for item in finding.related_requirement_ids)
        lines.append(f"- related_requirement_ids: {related}")
    if finding.details:
        details = yaml.safe_dump(finding.details, sort_keys=False, allow_unicode=True).rstrip()
        lines.extend(["", "```yaml", details, "```"])
    lines.append("")
    return lines


def _display_value(value: str | None) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value)


def _coerce_result(value: list[Finding] | ElicitResult) -> ElicitResult:
    if isinstance(value, ElicitResult):
        return value
    return ElicitResult(findings=list(value))


def _coverage_section(result: ElicitResult) -> str:
    if not result.lexicon_coverage_report:
        return ""
    lines = ["## Lexicon coverage", ""]
    for category, status in result.lexicon_coverage_report.items():
        lines.append(f"- `{category}`: **{status}**")
    lines.append("")
    return "\n".join(lines) + "\n"
