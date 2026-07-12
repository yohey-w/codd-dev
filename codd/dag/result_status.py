"""Canonical status/findings predicates for DAG check results.

A DAG check result is consumed by three independent summaries — the CLI verify
summaries (``codd/cli.py``), the coverage roll-up (``codd/coverage_metrics.py``)
and the deploy gate (``codd/deployer.py``). Each used to carry its **own** copy
of "does this result have findings?", and the copies drifted: only the CLI copy
checked the result's declared ``status`` and counted ``warnings`` / ``findings``.
The consequence was a visibility false-green — an amber result with
``status="warn"`` and advisory ``warnings``/``findings`` was counted as a *clean*
pass by coverage and deploy, so a real WARN silently vanished from those views.

This module is the single source of truth. It is deliberately a set of **pure
functions** with no heavy ``codd`` dependency (only ``typing``) — exactly like
``codd.dag.materiality`` — so the three consumers can import it without any risk
of a circular import.

Backward compatibility: a true clean pass (no non-pass status, no findings)
still returns ``has_findings() is False`` / ``pass_is_warn() is False``; the only
behavioral change is that a warn-bearing amber result is now correctly surfaced
as a finding everywhere, not just in the CLI.
"""

from __future__ import annotations

from typing import Any

# A check's own declared non-pass status — the most robust findings signal,
# independent of which field name a check uses to carry its findings.
_NON_PASS_STATUSES = {"warn", "warning", "fail", "failed"}

# Field names under which a check may report findings/advisories. ``warnings`` and
# ``findings`` are included so an amber-via-warnings/findings check is counted and
# shown, never rendered as a clean PASS.
_FINDING_KEYS = (
    "violations",
    "warnings",
    "findings",
    "missing_impl_files",
    "orphan_edges",
    "dangling_refs",
    "incomplete_tasks",
    "unreachable_nodes",
)


def result_value(result: Any, key: str) -> Any:
    """Read ``key`` off a result that may be a dataclass/object or a dict."""
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)


def result_severity(result: Any) -> str:
    return str(result_value(result, "severity") or "red")


def result_status(result: Any) -> str:
    return str(result_value(result, "status") or "")


def result_passed(result: Any) -> bool:
    """True ONLY when the result explicitly declares ``passed is True``.

    Anti-false-green: a result that omits ``passed`` (dict without the key, or an
    object whose attribute is missing), or that carries ``passed=None`` — e.g. a
    check that malfunctioned or produced no verdict — verified nothing reliable
    and must NOT read as a clean PASS. The old ``is not False`` predicate treated
    ``None``/missing as pass, so a check returning ``None`` or a verdict-less
    result rendered green in the CLI summary, inflated coverage, and slipped past
    the deploy gate. Legitimate ``skip``/``opt_out`` semantics are unaffected: a
    real :class:`CheckResult` sets ``passed=True`` for those states (and they are
    still filtered by ``status`` in the consumers).
    """
    return result_value(result, "passed") is True


def result_has_findings(result: Any) -> bool:
    """True iff the result carries anything that must not render as a clean PASS.

    A check's own declared non-pass status (``warn`` / ``fail`` …) is the most
    robust signal — it does not depend on which field name the check uses for its
    findings. Failing that, any non-empty findings field counts.
    """
    if result_status(result) in _NON_PASS_STATUSES:
        return True
    for key in _FINDING_KEYS:
        if result_value(result, key):
            return True
    return False


def pass_is_warn(result: Any) -> bool:
    """A passed amber check that carries findings must be surfaced as WARN.

    Shared by every verify/coverage/deploy summary so an amber-with-findings
    result that reports ``passed=True`` is never hidden behind a green-looking
    PASS. Backward compatible: a finding-free amber pass stays PASS.
    """
    return result_severity(result) == "amber" and result_has_findings(result)
