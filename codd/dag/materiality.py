"""Materiality overlay for DAG check results.

A check can report ``status == "pass"`` having verified **zero** items — e.g.
``ui_coherence_for_one_to_many`` on a project with no one-to-many relations. Such
a *vacuous* pass is indistinguishable, in a verify summary, from a pass that
actually checked something. This overlay reads an optional ``checked_count`` off
any result and separates the two, so a run riddled with vacuous passes is visibly
not a full verification.

The overlay is deliberately generic: it carries no per-check, project, framework
or language literal (a check exposes its own ``checked_count``), and it runs as a
post-processing overlay (CLI summary / JSON), never as a registered check — a
registered meta-check could itself be de-selected by ``enabled_checks`` and go
silent, which is exactly the failure mode it guards against.
"""

from __future__ import annotations

from typing import Any

_PASS_STATUSES = {"pass", "passed", "ok"}


def _status(result: Any) -> str:
    return str(getattr(result, "status", "") or "").lower()


def is_vacuous_pass(result: Any) -> bool:
    """True iff ``result`` passed but verified zero items.

    Guards (each prevents a false positive):
    - a *skipped* check verified nothing on purpose — not vacuous;
    - only ``pass``-family statuses qualify (a fail/warn is already visible);
    - a legacy result that does not report ``checked_count`` is left alone
      (``checked_count is None`` → not flagged), preserving backward compat.
    """
    if getattr(result, "skipped", False):
        return False
    if _status(result) not in _PASS_STATUSES:
        return False
    count = getattr(result, "checked_count", None)
    return isinstance(count, int) and not isinstance(count, bool) and count == 0


def vacuous_pass_results(results: Any) -> list:
    """Return the subset of ``results`` that are vacuous passes."""
    return [result for result in results if is_vacuous_pass(result)]
