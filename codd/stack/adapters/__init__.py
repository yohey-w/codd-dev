"""Stack-layer obligation checkers + a ref→callable registry.

A profile obligation declares a ``checker`` ref string (e.g.
``nextjs_adapter:check_ignore_build_errors``). This registry maps that ref to the
callable that ENFORCES it — which is what makes a declared obligation real
enforcement rather than declarative theater. The framework-conformance contract
(tests) requires every ERROR-severity obligation to resolve to a registered
checker, so an unenforced release-blocker fails CI.
"""

from __future__ import annotations

from . import nextjs, playwright, prisma
from ._base import ObligationFinding

#: obligation.checker ref (as written in a profile YAML) -> enforcing callable.
OBLIGATION_CHECKERS = {
    "nextjs_adapter:check_ignore_build_errors": nextjs.check_ignore_build_errors,
    "nextjs_adapter:check_route_coverage": nextjs.check_route_coverage,
    "playwright_adapter:check_executed": playwright.check_executed,
    "prisma_adapter:check_schema_sync": prisma.check_schema_sync,
}


def resolve_checker(ref: str | None):
    """Resolve an obligation ``checker`` ref string to its callable, or ``None``."""
    if not ref:
        return None
    return OBLIGATION_CHECKERS.get(str(ref))


__all__ = [
    "ObligationFinding",
    "OBLIGATION_CHECKERS",
    "resolve_checker",
    "nextjs",
    "playwright",
    "prisma",
]
