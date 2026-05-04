"""Design-token drift fix strategy."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from codd.fixup_drift_strategies import BaseFixStrategy, FixProposal, register_strategy


@register_strategy
class DesignTokenDriftFixStrategy(BaseFixStrategy):
    """Propose fixes for design-token drift events."""

    KIND = "design_token_drift"

    def propose(self, event: Any) -> list[FixProposal]:
        payload = getattr(event, "payload", {}) or {}
        token_name = self._first_payload_value(
            payload,
            "token_name",
            "token",
            "property",
            default="<unknown>",
        )
        old_value = self._first_payload_value(
            payload,
            "old_value",
            "actual_value",
            "actual",
            "pattern",
            "token",
            default="<unknown>",
        )
        new_value = self._first_payload_value(
            payload,
            "new_value",
            "expected_value",
            "expected",
            "suggestion",
            default="<unknown>",
        )
        file_path = self._first_payload_value(
            payload,
            "file",
            "path",
            "location",
            default=str(getattr(event, "source_artifact", "<unknown>")),
        )
        severity = str(getattr(event, "severity", "amber"))

        can_auto_apply = self._is_case_normalization(old_value, new_value)
        if can_auto_apply:
            description = (
                f"Design token case normalization: {token_name!r} "
                f"{old_value!r} -> {new_value!r}."
            )
        else:
            description = (
                f"Design token value changed: {token_name!r} "
                f"{old_value!r} -> {new_value!r}. Requires human review."
            )

        return [
            FixProposal(
                kind=self.KIND,
                file_path=file_path,
                diff=f"-{old_value}\n+{new_value}",
                description=description,
                severity=severity,
                can_auto_apply=can_auto_apply,
            )
        ]

    def apply(self, proposal: FixProposal) -> bool:
        """Automatically apply safe case-normalization proposals only."""
        if not proposal.can_auto_apply:
            return False

        old_value, new_value = self._diff_values(proposal.diff)
        if not old_value or not new_value:
            return False

        path = self._proposal_path(proposal.file_path)
        if path is None or not path.exists() or not path.is_file():
            return False

        content = path.read_text(encoding="utf-8")
        if old_value not in content:
            return False

        path.write_text(content.replace(old_value, new_value), encoding="utf-8")
        return True

    def _is_case_normalization(self, old_value: str, new_value: str) -> bool:
        """Return true when two token names differ only by naming case."""

        def normalize(value: str) -> str:
            value = value.strip().lstrip("-")
            value = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
            value = re.sub(r"[_\s.]+", "-", value)
            value = re.sub(r"-+", "-", value)
            return value.strip("-").lower()

        unknown_values = {"<unknown>", "unknown"}
        if old_value in unknown_values or new_value in unknown_values:
            return False
        return bool(old_value and new_value) and normalize(old_value) == normalize(new_value)

    def _proposal_path(self, file_path: str) -> Path | None:
        path = Path(file_path)
        if path.is_absolute():
            try:
                path.resolve().relative_to(self.project_root.resolve())
            except ValueError:
                return None
            return path
        return self.project_root / path

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

    @staticmethod
    def _diff_values(diff: str) -> tuple[str | None, str | None]:
        old_value = None
        new_value = None
        for line in diff.splitlines():
            if line.startswith("-") and not line.startswith("--- "):
                old_value = line[1:]
            elif line.startswith("+") and not line.startswith("+++ "):
                new_value = line[1:]
        return old_value, new_value
