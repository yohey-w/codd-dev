"""Lexicon violation fix strategy."""

from __future__ import annotations

from typing import Any

from codd.fixup_drift_strategies import BaseFixStrategy, FixProposal, register_strategy


@register_strategy
class LexiconViolationFixStrategy(BaseFixStrategy):
    """Create HITL-only proposals for lexicon violations."""

    KIND = "lexicon_violation"

    def propose(self, event: Any) -> list[FixProposal]:
        payload = getattr(event, "payload", {}) or {}
        term = self._first_payload_value(
            payload,
            "term",
            "node_id",
            "actual",
            "found",
            "location",
            default="<unknown>",
        )
        violation_type = self._first_payload_value(
            payload,
            "violation_type",
            "rule",
            "code",
            "check",
            default="unknown",
        )
        file_path = self._first_payload_value(
            payload,
            "file",
            "path",
            "location",
            default=str(getattr(event, "source_artifact", "<unknown>")),
        )
        severity = str(getattr(event, "severity", "amber"))

        return [
            FixProposal(
                kind=self.KIND,
                file_path=file_path,
                diff=(
                    f"# Lexicon term: {term}\n"
                    f"# Violation: {violation_type}\n"
                    "# TODO: update the term or project lexicon after human review"
                ),
                description=(
                    f"Lexicon violation: term={term!r}, type={violation_type!r}. "
                    "Requires human review."
                ),
                severity=severity,
                can_auto_apply=False,
            )
        ]

    def apply(self, proposal: FixProposal) -> bool:
        """Lexicon changes always require human review."""
        return False

    @staticmethod
    def _first_payload_value(
        payload: dict[str, Any],
        *keys: str,
        default: str,
    ) -> str:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        return default
