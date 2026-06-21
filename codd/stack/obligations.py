"""Run a resolved stack contract's obligations through their registered checkers.

This is the GATE SEAM: greenfield/verify call :func:`enforce_obligations` with a
:class:`~codd.stack.compose.ResolvedStackContract` and the project context; each
obligation is dispatched to its registered checker (design §3 obligations). An
ERROR-severity obligation with findings is gate-blocking; a WARN one is advisory.

Anti-false-green (cardinal rule): a checker that is *not real enforcement* must
NEVER be read as "satisfied". Two distinct failure modes are surfaced, both
gate-blocking for an ERROR obligation (Contract Kernel v2.77e):

* ``unenforced`` — the obligation declares a checker ref that resolves to NOTHING
  (missing ref / null / unregistered / a registry entry that is not callable). The
  enforcement does not exist; an ERROR release-blocker with no checker is a failure,
  never a silent pass.
* ``faults`` — the checker EXISTS but did not produce a usable verdict: it raised,
  returned ``None`` (the canonical ``checker(...) or []`` false-green this layer
  fixes — an unimplemented checker that falls off the end returns ``None``, which
  must NOT collapse to "no findings"), or returned a non-list malformed value. The
  obligation could not be honestly judged → RED for an ERROR obligation.

A WARN obligation that is unenforced or faults is advisory (does not block) — but is
still reported so the run record is honest. The conformance contract
(``tests/test_stack_obligations.py``) keeps the curated profiles' ERROR obligations
enforceable; a runtime caller must still surface these honestly.
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
class ObligationFault:
    """A checker that EXISTS but produced no usable verdict (raised / None / malformed).

    Distinct from a :class:`ObligationViolation` (the checker ran and found a real
    violation) and from ``unenforced`` (no checker at all): here the checker was
    invoked but could not be honestly read as "satisfied". For an ERROR obligation
    this is RED — the obligation could not be enforced (anti-false-green: never treat
    a broken checker as a pass).
    """

    obligation: Obligation
    reason: str

    @property
    def blocking(self) -> bool:
        return self.obligation.severity == "error"


@dataclass(frozen=True)
class ObligationResult:
    violations: tuple[ObligationViolation, ...]
    unenforced: tuple[Obligation, ...]
    faults: tuple[ObligationFault, ...] = ()

    @property
    def blocking_violations(self) -> tuple[ObligationViolation, ...]:
        return tuple(v for v in self.violations if v.blocking)

    @property
    def blocking_faults(self) -> tuple[ObligationFault, ...]:
        return tuple(f for f in self.faults if f.blocking)

    @property
    def passed(self) -> bool:
        """Gate verdict: GREEN only when every ERROR obligation was genuinely
        enforced AND satisfied. RED on ANY of: a blocking violation; an unenforced
        ERROR obligation (no checker — claimed enforcement that does not run); a
        blocking fault (the checker exists but raised / returned None / returned a
        malformed value — the obligation could not be honestly judged). All three are
        anti-false-green: a release-blocker that was not really checked is never green."""
        if self.blocking_violations:
            return False
        if self.blocking_faults:
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

    Hardened seam (v2.77e): a registered checker is invoked defensively. It must
    return a ``list`` of findings (an empty list == SATISFIED). A checker that
    raises, returns ``None``, or returns a non-list is recorded as an
    :class:`ObligationFault` (NOT silently treated as satisfied) — the ``or []``
    coercion that turned an unimplemented (``None``-returning) checker into a false
    green is removed. A registry entry that is not callable is treated as
    ``unenforced`` (no usable checker exists).
    """
    violations: list[ObligationViolation] = []
    unenforced: list[Obligation] = []
    faults: list[ObligationFault] = []
    for obl in contract.obligations:
        checker = resolve_checker(obl.checker)
        if checker is None:
            unenforced.append(obl)
            continue
        if not callable(checker):
            # A registry entry that is not callable cannot enforce anything — there is
            # no usable checker, so this is "unenforced", not a fault (same verdict for
            # an ERROR obligation: RED).
            unenforced.append(obl)
            continue
        try:
            findings = checker(project_root=project_root, **checker_inputs)
        except Exception as exc:  # noqa: BLE001 — a crashing checker is NOT "satisfied".
            faults.append(
                ObligationFault(
                    obligation=obl,
                    reason=f"checker {obl.checker!r} raised {type(exc).__name__}: {exc}",
                )
            )
            continue
        if findings is None:
            faults.append(
                ObligationFault(
                    obligation=obl,
                    reason=(
                        f"checker {obl.checker!r} returned None (no verdict) — an "
                        "unimplemented or fall-through checker is not a satisfied "
                        "obligation (anti-false-green: never coerce None to no-findings)"
                    ),
                )
            )
            continue
        if not isinstance(findings, list):
            faults.append(
                ObligationFault(
                    obligation=obl,
                    reason=(
                        f"checker {obl.checker!r} returned a non-list "
                        f"{type(findings).__name__} — a checker must return a list of "
                        "findings (empty == satisfied); a malformed return is not a pass"
                    ),
                )
            )
            continue
        for finding in findings:
            violations.append(ObligationViolation(obligation=obl, finding=finding))
    return ObligationResult(
        violations=tuple(violations),
        unenforced=tuple(unenforced),
        faults=tuple(faults),
    )
