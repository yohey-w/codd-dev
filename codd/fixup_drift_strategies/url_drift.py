"""URL drift fix strategy.

URL changes are human-in-the-loop only because redirects, external links, and
versioning need explicit review. The fixup-drift apply path records these
proposals in ``pending_hitl.md`` instead of applying them automatically.
"""

from __future__ import annotations

from codd.fixup_drift_strategies import BaseFixStrategy, FixProposal, register_strategy


@register_strategy
class UrlDriftFixStrategy(BaseFixStrategy):
    """Build HITL-only fix proposals for URL drift events."""

    KIND = "url_drift"

    def propose(self, event) -> list[FixProposal]:
        """Create a non-auto proposal from a URL drift event."""
        payload = getattr(event, "payload", {}) or {}
        old_url = payload.get("old_url", "<unknown>")
        new_url = payload.get("new_url", "<unknown>")
        file_path = getattr(event, "source_artifact", "<unknown>")
        severity = getattr(event, "severity", "amber")

        return [
            FixProposal(
                kind=self.KIND,
                file_path=str(file_path),
                diff=f"-{old_url}\n+{new_url}",
                description=(
                    f"URL changed: {old_url!r} -> {new_url!r}. "
                    "Human verification required (redirect/versioning)."
                ),
                severity=severity,
                can_auto_apply=False,
            )
        ]

    def apply(self, proposal: FixProposal) -> bool:
        """URL drift is never applied automatically."""
        return False
