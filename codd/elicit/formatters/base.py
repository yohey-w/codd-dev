"""Formatter protocol for elicit findings."""

from __future__ import annotations

from typing import Protocol

from codd.elicit.finding import Finding


class FindingFormatter(Protocol):
    name: str

    def format(self, findings: list[Finding]) -> str:
        ...

    def parse_approval(self, raw: str) -> list[str]:
        """Parse user-approved finding IDs from input."""
        ...
