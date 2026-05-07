"""JSON formatter for elicit findings."""

from __future__ import annotations

import json
from typing import Any

from codd.elicit.finding import Finding


class JsonFormatter:
    name = "json"

    def format(self, findings: list[Finding]) -> str:
        return json.dumps([finding.to_dict() for finding in findings], indent=2, sort_keys=True) + "\n"

    def parse_approval(self, raw: str) -> list[str]:
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise ValueError("Approval input must be a JSON array of finding IDs")
        if not all(isinstance(item, str) for item in payload):
            raise ValueError("Approval input must contain only finding ID strings")
        return list(payload)


def finding_to_jsonable(finding: Finding) -> dict[str, Any]:
    return finding.to_dict()
