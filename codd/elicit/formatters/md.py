"""Markdown formatter for elicit findings."""

from __future__ import annotations

import json
import re

import yaml

from codd.elicit.finding import Finding


_CHECKED_APPROVAL_RE = re.compile(r"^\s*-\s*approval:\s*\[[xX]\]\s*`?([^`\s]+)`?", re.MULTILINE)


class MdFormatter:
    name = "md"

    def format(self, findings: list[Finding]) -> str:
        if not findings:
            return "# Findings\n\nNo findings.\n"

        lines = ["# Findings", ""]
        for finding in findings:
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
