"""CoDD fix — PHENOMENON-driven (operational feedback) mode.

Implements the second entry point of CoDD's north star:
"Given a phenomenon the user wants fixed, CoDD updates the design doc,
implementation, and tests in one shot — the user touches nothing."

The existing `codd fix` (argument-less, test/CI failure driven) is the
first entry point. This package adds the second one as an additive,
opt-in code path. The legacy run_fix() is not modified.
"""

from codd.fix.phenomenon_fixer import (
    PhenomenonFixResult,
    PhenomenonFixAttempt,
    run_phenomenon_fix,
)

__all__ = [
    "PhenomenonFixResult",
    "PhenomenonFixAttempt",
    "run_phenomenon_fix",
]
