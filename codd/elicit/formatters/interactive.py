"""Inline CLI formatter for elicit findings."""

from __future__ import annotations

from collections.abc import Callable
import re

from codd.elicit.finding import ElicitResult, Finding


_APPROVE = {"a", "approve", "approved", "y", "yes"}
_REJECT = {"n", "no", "r", "reject", "rejected"}
_DEFER = {"d", "defer", "deferred", "later"}


class InteractiveFormatter:
    name = "interactive"

    def __init__(self) -> None:
        self._last_ids: list[str] = []

    def format(self, findings: list[Finding] | ElicitResult) -> str:
        if isinstance(findings, ElicitResult):
            findings = findings.findings
        self._last_ids = [finding.id for finding in findings]
        if not findings:
            return "No findings.\n"

        blocks: list[str] = []
        for index, finding in enumerate(findings, start=1):
            lines = [
                f"[{index}/{len(findings)}] {finding.id}",
                f"kind: {finding.kind}",
                f"severity: {finding.severity}",
            ]
            if finding.name:
                lines.append(f"name: {finding.name}")
            if finding.question:
                lines.append(f"question: {finding.question}")
            if finding.rationale:
                lines.append(f"rationale: {finding.rationale}")
            lines.append("Approve? [Y/n/d]")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) + "\n"

    def parse_approval(self, raw: str) -> list[str]:
        responses = [line.strip() for line in raw.splitlines() if line.strip()]
        if not responses:
            return []

        if any(":" in line for line in responses):
            return _parse_keyed_responses(responses)

        if self._last_ids and len(responses) == len(self._last_ids):
            approved: list[str] = []
            for finding_id, response in zip(self._last_ids, responses):
                decision = _normalize_decision(response)
                if decision == "approve":
                    approved.append(finding_id)
            return approved

        return [token.strip() for token in re.split(r"[\s,]+", raw.strip()) if token.strip()]

    def collect_approvals(
        self,
        findings: list[Finding],
        *,
        input_func: Callable[[str], str] = input,
        output_func: Callable[[str], None] = print,
    ) -> list[str]:
        approved: list[str] = []
        self._last_ids = [finding.id for finding in findings]
        for index, finding in enumerate(findings, start=1):
            output_func(self.format([finding]).rstrip())
            response = input_func(f"[{index}/{len(findings)}] {finding.id} [Y/n/d]: ")
            decision = _normalize_decision(response)
            if decision == "approve":
                approved.append(finding.id)
        self._last_ids = [finding.id for finding in findings]
        return approved


def _parse_keyed_responses(responses: list[str]) -> list[str]:
    approved: list[str] = []
    for line in responses:
        finding_id, raw_decision = line.split(":", 1)
        if _normalize_decision(raw_decision) == "approve":
            approved.append(finding_id.strip())
    return approved


def _normalize_decision(raw: str) -> str:
    value = raw.strip().lower()
    if value == "":
        return "approve"
    if value in _APPROVE:
        return "approve"
    if value in _REJECT:
        return "reject"
    if value in _DEFER:
        return "defer"
    return "approve"
