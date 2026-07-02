"""Implement-oracle value-types — the LEAF module both the core gate and the
Contract-Kernel oracle adapters import (Contract Kernel oracle dispatch §3).

WHY A LEAF MODULE
=================
The oracle dispatch is moving toward ``LanguageProfile``-driven adapters (a
``go-toolchain`` / ``typescript-tsc`` / ``python-composite`` adapter resolved from
the profile). Those adapters — and the generic command-sequence executor that
drives them — must produce the SAME ``ImplementOracleResult`` /
``ImplementOracleFinding`` the core gate (:mod:`codd.implement_oracle`) consumes,
and must raise the SAME ``OracleScopeError``. If those value-objects stayed in the
gate module, an adapter importing them would create an import cycle
(gate → adapter → gate). So they live HERE, in a leaf that imports only stdlib;
the gate re-imports + RE-EXPORTS them (identity preserved — every existing
``from codd.implement_oracle import ImplementOracleResult`` keeps working and gets
the SAME class object), and the adapters import them straight from this leaf.

This mirrors the ``RunnerExecution`` relocation (v2.71): the value-objects moved to
:mod:`codd.languages.adapters.runner_report` (a leaf) and
:mod:`codd.coverage_execution_coherence` re-exports them, so both the gate and the
adapters share ONE definition with no cycle. ZERO behaviour change — this is a pure
relocation; the bodies are byte-for-byte what they were in the gate module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── evidence categories (language-neutral; the design's normalization target) ──
#
# The whole point of normalization: a Go/Rust adapter later emits the SAME
# vocabulary so the SUT-facing feedback and any downstream policy are stack-
# agnostic. ``boundary_violation`` (an e2e/modality contract breach) is in the
# vocabulary for completeness but is NOT something a pure typechecker emits — the
# existing AST e2e-contract gate owns that axis; it is here so the category set is
# the full design set and a future composite/boundary adapter can use it.
EVIDENCE_MISSING_SYMBOL = "missing_symbol"
EVIDENCE_MODULE_RESOLUTION = "module_resolution_error"
EVIDENCE_TEST_NOT_COLLECTED = "test_not_collected"
EVIDENCE_ENVIRONMENT_BUILD = "environment_build_error"
EVIDENCE_BOUNDARY_VIOLATION = "boundary_violation"
EVIDENCE_OTHER = "type_error"  # a real coherence error not in the categories above

EVIDENCE_CATEGORIES: tuple[str, ...] = (
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    EVIDENCE_TEST_NOT_COLLECTED,
    EVIDENCE_ENVIRONMENT_BUILD,
    EVIDENCE_BOUNDARY_VIOLATION,
    EVIDENCE_OTHER,
)


#: How many individual diagnostics to surface in SUT feedback (bounded prompt).
_FEEDBACK_FINDING_CAP = 12


class OracleScopeError(RuntimeError):
    """The oracle's scope could not be CERTIFIED to cover source + tests.

    Raised when ``tsconfig.json`` is missing/unparseable, when its
    ``include``/``files`` provably excludes the test tree (or the source tree),
    or when ``tsc`` reports it found no inputs. An uncertifiable scope is a HARD
    FAIL — a green oracle over an unknown scope is a false-green, the #1 failure
    mode the design calls out.
    """


@dataclass(frozen=True)
class ImplementOracleFinding:
    """One normalized oracle diagnostic."""

    category: str
    code: str
    message: str
    path: str | None = None


@dataclass
class ImplementOracleResult:
    """Outcome of one implement-time oracle run (one ``tsc`` invocation)."""

    passed: bool
    executed: bool
    command: str
    findings: list[ImplementOracleFinding] = field(default_factory=list)
    #: EDITABLE source/test targets the failure attributes to (for diagnostics).
    failed_paths: list[str] = field(default_factory=list)
    detail: str = ""
    raw_output: str = ""
    #: Structured diagnostics (code + primary file + symbol/module) for the SCOPED
    #: rerun derivation + the loop-breaking signature. Empty for a pass / for a
    #: non-TS oracle. Kept separate from ``findings`` (the SUT-facing normalized
    #: evidence) because scope derivation needs the per-diagnostic counterpart
    #: keys, not the language-neutral category. See ``codd.implement_oracle_scope``.
    diagnostics: list[Any] = field(default_factory=list)
    #: Orphan artifacts (generated source files no task owns) found by the global
    #: orphan-artifact gate. Populated in WARN mode (observation; the gate does not
    #: fail) and ENFORCE mode (the gate fails). Empty when the gate is off / no
    #: scope index / no orphans. Project-relative paths, for the report + dashboard.
    orphan_artifacts: list[str] = field(default_factory=list)

    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.category] = counts.get(finding.category, 0) + 1
        return counts

    def feedback_message(self) -> str:
        """SUT-facing feedback: what the native oracle proved is incoherent.

        Deliberately concrete and bounded — it names the offending files, the
        diagnostic codes, and the language-neutral category, then directs the SUT
        to make the demanded symbols/modules exist. It NEVER prescribes a verify
        fix (the SUT owns the contents); it states the contract that was broken.
        """
        lines = [
            "The implement-time coherence oracle REJECTED the generated code: "
            "independently-generated files do not agree on the syntax, module "
            "imports, or imported symbols they demand of each other. Nothing was "
            "accepted. (TypeScript runs `tsc --noEmit`; Python runs an in-process "
            "compile + a first-party import/symbol resolver + `pytest "
            "--collect-only`; JavaScript runs `node --check` per file + a "
            "first-party import/export resolver.)",
            "Fix the source AND tests so every file compiles, every import "
            "resolves, and every imported symbol is actually defined/exported by "
            "its module. Specifically:",
        ]
        for finding in self.findings[:_FEEDBACK_FINDING_CAP]:
            where = f"{finding.path}: " if finding.path else ""
            lines.append(f"  - [{finding.category}] {where}{finding.code}: {finding.message}")
        extra = len(self.findings) - _FEEDBACK_FINDING_CAP
        if extra > 0:
            lines.append(f"  ... and {extra} more diagnostic(s).")
        lines.append(
            "Ensure: (a) every imported name actually exists in its module — "
            'TypeScript `import { X } from "./mod.js"` imports a name `X` that '
            "`mod` actually `export`s, and Python `from app.mod import X` imports "
            "an `X` that `app/mod.py` actually defines or re-exports; (b) "
            "test/helper files agree on the names they share (e.g. do not import "
            "`repoRoot` when the helper exports `projectRoot`); (c) every imported "
            "module path resolves (no `from .missing import ...` to a module that "
            "does not exist)."
        )
        # (iii) Contract discipline — the anti-oscillation rule. A non-deterministic
        # SUT, told only "symbol X is missing", tends to INVENT a new symbol/file
        # each rerun (a different wrong guess every time → the diagnostics oscillate
        # instead of converging). Forbid invention explicitly: reconcile to what
        # ALREADY exists; a genuinely shared symbol goes in an OWNED module, not a
        # newly-conjured file. (GPT-5.5 Pro consult, 2026-06-15: feedback iii.)
        lines.append(
            "Do NOT invent new symbols, helpers, or files to satisfy an import: "
            "reconcile the import to a symbol the target module ALREADY exports (or "
            "delete the import/usage if it is spurious). If two files must share a "
            "symbol, add it to one OWNED module and import it — never duplicate it or "
            "create an unowned shared file. Make the SMALLEST change that restores "
            "coherence; do not rewrite unrelated code."
        )
        return "\n".join(lines)


__all__ = [
    "EVIDENCE_BOUNDARY_VIOLATION",
    "EVIDENCE_CATEGORIES",
    "EVIDENCE_ENVIRONMENT_BUILD",
    "EVIDENCE_MISSING_SYMBOL",
    "EVIDENCE_MODULE_RESOLUTION",
    "EVIDENCE_OTHER",
    "EVIDENCE_TEST_NOT_COLLECTED",
    "ImplementOracleFinding",
    "ImplementOracleResult",
    "OracleScopeError",
]
