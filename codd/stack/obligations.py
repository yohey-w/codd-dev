"""Run a resolved stack contract's obligations through their registered checkers.

This is the GATE SEAM: greenfield/verify call :func:`enforce_obligations` with a
:class:`~codd.stack.compose.ResolvedStackContract` and the project context; each
obligation is dispatched to its registered checker (design §3 obligations). An
ERROR-severity obligation with findings is gate-blocking; a WARN one is advisory.

An ERROR-severity obligation with NO registered checker is reported as
``unenforced`` — never silently passed (that would be a false green: claiming
enforcement that does not run). The conformance contract
(``tests/test_stack_obligations.py``) keeps ``unenforced`` empty for the curated
profiles, but a runtime caller must still surface it honestly.
"""

from __future__ import annotations

from dataclasses import dataclass

from .adapters import resolve_checker
from .adapters._base import ObligationFinding
from .compose import ResolvedStackContract
from .profile import Obligation


@dataclass(frozen=True)
class ObligationViolation:
    obligation: Obligation
    finding: ObligationFinding

    @property
    def blocking(self) -> bool:
        return self.obligation.severity == "error"


@dataclass(frozen=True)
class ObligationResult:
    violations: tuple[ObligationViolation, ...]
    unenforced: tuple[Obligation, ...]

    @property
    def blocking_violations(self) -> tuple[ObligationViolation, ...]:
        return tuple(v for v in self.violations if v.blocking)

    @property
    def passed(self) -> bool:
        """Gate verdict: no blocking violation AND no unenforced error obligation
        (an unenforced release-blocker is itself a failure — honest, not silent)."""
        if self.blocking_violations:
            return False
        return not any(o.severity == "error" for o in self.unenforced)


def enforce_obligations(
    contract: ResolvedStackContract,
    *,
    project_root=None,
    **checker_inputs,
) -> ObligationResult:
    """Dispatch each obligation to its registered checker and collect the result.

    ``checker_inputs`` (e.g. ``report_data=...`` / ``report_path=...``) are passed
    through to every checker; each checker takes ``**kwargs`` and uses what it
    needs (the Next.js guard reads ``project_root``; the Playwright guard reads
    the report).
    """
    violations: list[ObligationViolation] = []
    unenforced: list[Obligation] = []
    for obl in contract.obligations:
        checker = resolve_checker(obl.checker)
        if checker is None:
            unenforced.append(obl)
            continue
        findings = checker(project_root=project_root, **checker_inputs) or []
        for finding in findings:
            violations.append(ObligationViolation(obligation=obl, finding=finding))
    return ObligationResult(violations=tuple(violations), unenforced=tuple(unenforced))
