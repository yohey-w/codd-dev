"""Playwright obligation checker — enforces the addon's e2e_actually_executed
obligation: a green Playwright result requires >=1 test ACTUALLY executed. A
fully-skipped or empty run is not e2e evidence (the same anti-false-green rule
the language runner-report adapters apply to skipped/missing tests).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ._base import ObligationFinding

_OBLIGATION = "e2e_actually_executed"


def _executed_count(stats: Mapping[str, Any]) -> int:
    """Tests that produced a real pass/fail outcome (skips/empty excluded)."""
    return int(stats.get("expected", 0)) + int(stats.get("unexpected", 0)) + int(stats.get("flaky", 0))


def check_executed(
    report_path: str | Path | None = None,
    report_data: Mapping[str, Any] | None = None,
    **_: object,
) -> list[ObligationFinding]:
    """Return findings if the Playwright run executed zero tests.

    Accepts either a parsed ``report_data`` mapping (Playwright JSON reporter) or
    a ``report_path`` to read. A missing/unreadable report is itself a violation
    (no execution evidence — never a silent pass).
    """
    data = report_data
    if data is None:
        if not report_path or not Path(report_path).exists():
            return [ObligationFinding(_OBLIGATION, str(report_path or "<none>"), "no Playwright report (no execution evidence)")]
        try:
            data = json.loads(Path(report_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [ObligationFinding(_OBLIGATION, str(report_path), f"unreadable Playwright report: {exc}")]

    stats = data.get("stats", {}) if isinstance(data, Mapping) else {}
    executed = _executed_count(stats)
    if executed == 0:
        return [
            ObligationFinding(
                _OBLIGATION,
                "<report>",
                f"0 tests executed (skipped={stats.get('skipped', 0)}) — a skipped/empty e2e run is not green",
            )
        ]
    return []
