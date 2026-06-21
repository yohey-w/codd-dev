"""Implement-time native-oracle gate — the "first head" of the Artifact Contract
Graph → Native Oracle Adapter (design: memory/project_codd_language_generality_acg,
two GPT-5.5 Pro consults, 2026-06-14).

WHAT
====
A compiler-class stack (TypeScript = ``tsc --noEmit``; later Go = ``go build``,
Rust = ``cargo check``) can statically PROVE that independently-generated
artifacts agree on the names/paths/symbols they demand of each other — BEFORE a
single line of code runs. This module runs that native oracle during the
greenfield IMPLEMENT stage, AFTER every unit is generated but BEFORE the run
advances to verify.

WHY IMPLEMENT-TIME (the load-bearing decision)
==============================================
The verify stage ALREADY runs ``tsc --noEmit`` (see
``codd/repair/verify_runner.py``: a node project with a ``tsconfig.json`` gets
the implicit ``npx --no-install tsc --noEmit`` typecheck). It catches the
incoherence — but TOO LATE: at verify the auto-repair is HITL-rejected /
scope-blocked and may NOT rewrite test files, so a test that imports ``repoRoot``
while the helper exports ``projectRoot`` (or ``src/index.ts`` importing a
``runCli`` that ``./cli`` never exports → TS2305/2724/2459) is a PERMANENT verify
failure. Moving the SAME oracle EARLIER — into implement, where the SUT can still
freely edit ALL files (source AND tests) — lets the model make symbols coherent
before verify ever gates. This is GPT-validated and mirrors PC-d4 (the VB
coverage gate moved from per-task to STAGE level for the same forward-reference
reason; see ``codd/greenfield/pipeline.py``).

GRANULARITY: STAGE-LEVEL (not per-unit / per-task)
==================================================
A PER-UNIT ``tsc`` would false-fail on a forward reference to a not-yet-generated
unit (``src/index.ts`` importing ``./cli`` before ``cli.ts`` exists). The gate
therefore runs ONCE, at the END of the implement stage, when every unit exists
and the whole module graph is coherently checkable. (See
``_enforce_stage_coverage_gate`` for the exact same once-per-stage shape.)

OBSERVABILITY CERTIFICATION (anti-false-green #1 failure mode)
=============================================================
A native oracle proves NOTHING about files outside its scope. Before trusting a
green ``tsc`` we CERTIFY that ``tsconfig.json``'s ``include``/``files`` actually
covers source + tests (which contain e2e + helpers). If the config is missing or
its scope excludes the test tree, the gate HARD-FAILS rather than passing
silently — the whole reason the gate exists is to catch test/helper incoherence.

EVIDENCE NORMALIZATION + BOUNDED RETRY
======================================
On oracle failure the diagnostics are normalized to language-neutral evidence
categories (``missing_symbol`` / ``module_resolution_error`` /
``test_not_collected`` / ``environment_build_error`` / ``boundary_violation``)
and fed back to the SUT via a bounded ``rerun(feedback)`` loop (the same shape as
``run_implement_coverage_gate``). Implement does not "succeed" until the oracle
passes or the bounded budget is spent — then it fails HONESTLY.

PROFILE-DRIVEN (not hardcoded)
==============================
The oracle command + scope come from
:class:`codd.project_types.ImplementOracleSpec` on the stack's
:class:`~codd.project_types.LayoutProfile`. A stack WITHOUT a declared oracle
(Python's composite is DEFERRED; bash; …) makes the gate a strict NO-OP — its
coherence backstop stays the existing verify-stage gates. A new compiler stack is
one profile entry + one evidence-normalizer entry here, never a core edit.

REUSE
=====
The oracle is RUN and ATTRIBUTED through the existing infrastructure:
``codd.repair.test_failure_attribution.attribute_command_failure`` (the same tsc
diagnostic parser verify uses), and the node-install preflight mirrors
``codd.project_types.node_install_command``. This module adds the implement-time
PLACEMENT, the SCOPE CERTIFICATION, the finer EVIDENCE CATEGORIES, and the
STAGE-level bounded-retry orchestration — it does not re-implement tsc running.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from codd.project_types import (
    ImplementOracleSpec,
    LayoutProfile,
    OracleScopeSpec,
    node_install_command,
    resolve_layout_profile,
)


__all__ = [
    "EVIDENCE_CATEGORIES",
    "ImplementOracleFinding",
    "ImplementOracleResult",
    "OracleRerunCallback",
    "OracleScopeError",
    "PythonOracleScope",
    "PythonToolRun",
    "build_contract_feedback",
    "certify_go_oracle_scope",
    "certify_oracle_scope",
    "certify_python_oracle_scope",
    "normalize_oracle_output",
    "normalize_python_tool_output",
    "resolve_implement_oracle",
    "run_implement_oracle_gate",
]

#: Internal-but-tested campaign entry point (the anti-false-green acceptance tests
#: drive it with a fake oracle + rerun). Not part of the stable public surface, but
#: importable for the gate's unit tests.
__test_exports__ = ["_execute_broad_campaign"]


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


#: TypeScript diagnostic codes → evidence category. The src↔src / test↔helper
#: symbol-coherence bugs the gate targets are exactly the "missing member /
#: cannot find name" family. ``module_resolution`` is the "cannot find module"
#: family. Mapping is conservative: an unmapped ``TSxxxx`` is a real type error
#: (``type_error``) — still a HARD failure, just not one of the named buckets.
_TS_MISSING_SYMBOL_CODES = frozenset(
    {
        "TS2305",  # Module '"X"' has no exported member 'Y'.
        "TS2724",  # '"X"' has no exported member named 'Y'. Did you mean 'Z'?
        "TS2459",  # Module '"X"' declares 'Y' locally, but it is not exported.
        "TS2614",  # Module '"X"' has no exported member 'Y'. (no default vs named)
        "TS2552",  # Cannot find name 'Y'. Did you mean 'Z'?
        "TS2304",  # Cannot find name 'Y'.
        "TS2339",  # Property 'Y' does not exist on type 'X'.
    }
)
_TS_MODULE_RESOLUTION_CODES = frozenset(
    {
        "TS2307",  # Cannot find module 'X' or its corresponding type declarations.
        "TS2792",  # Cannot find module 'X'. Did you mean to set 'moduleResolution'?
        "TS6053",  # File 'X' not found.
        "TS5083",  # Cannot read file 'X'.
    }
)

#: A tsc diagnostic line: ``path(line,col): error TSxxxx: message`` (pretty) or
#: ``path:line:col - error TSxxxx: message`` (``--pretty false``). Captures the
#: code + the trailing message so the per-error category + a compact SUT-facing
#: summary can be built without re-parsing.
_TS_DIAG_LINE = re.compile(
    r"error\s+(?P<code>TS\d+)\s*:\s*(?P<message>.+?)\s*$",
    re.MULTILINE,
)

#: ``tsc`` emits this when its include/files resolve to nothing — a config-scope
#: failure that must NEVER read as "0 errors → coherent" (it typechecked nothing).
_TS_NO_INPUTS_RE = re.compile(r"TS18003|No inputs were found in config file", re.IGNORECASE)


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
            "--collect-only`.)",
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


#: How many individual diagnostics to surface in SUT feedback (bounded prompt).
_FEEDBACK_FINDING_CAP = 12

#: Total implement-oracle attempts: one initial check + bounded corrective
#: retries, sized to the ESCALATION LADDER so every rung can actually be reached
#: before the budget is spent. Default 5 = initial(1) + narrow(≤2) + expanded(1)
#: + broad(1). A flat cap of 3 (the previous default) could not run all three
#: rungs once — it died on ``narrow`` (initial + 2 reruns), so an oscillating SUT
#: that needed ``expanded``/``broad`` never got there (2026-06-15 codex11 dogfood:
#: 20→4→6 oscillation forced the gate to give up at cap=3 while still narrow).
#: The second ``narrow`` attempt is only spent when progress is being made (see
#: the progress/oscillation escalation in ``run_implement_oracle_gate``);
#: oscillation escalates immediately, so the budget is not wasted thrashing one
#: rung. Bounded on purpose — a genuinely uncurable incoherence still fails
#: honestly (nothing is silently accepted). Override via
#: ``implement.oracle_max_attempts``. Mirrors the implement syntax-gate's
#: ``DEFAULT_SYNTAX_GATE_MAX_ATTEMPTS`` discipline.
DEFAULT_ORACLE_MAX_ATTEMPTS = 5


def _oracle_max_attempts(config: Mapping[str, Any] | None) -> int:
    """``implement.oracle_max_attempts`` — total attempts (>=1), else the default."""
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "oracle_max_attempts" in section:
        raw = section["oracle_max_attempts"]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_ORACLE_MAX_ATTEMPTS
        return value if value >= 1 else DEFAULT_ORACLE_MAX_ATTEMPTS
    return DEFAULT_ORACLE_MAX_ATTEMPTS


def _oracle_opt_out(config: Mapping[str, Any] | None) -> bool:
    """``implement.implement_oracle: false`` — the explicit opt-out.

    Default OFF (the gate runs). Opting out re-opens the false-green risk the
    gate closes, so it is never the default and never silent.
    """
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "implement_oracle" in section:
        return section["implement_oracle"] is False
    return False


# ── broad-repair campaign config (the budgeted residual coherence campaign) ──
#
# When a wide-fan-out artifact forces the broad RUNG, broad's EXECUTION is no
# longer "regenerate every task" (~17 tasks, ~40-50 min/attempt, wall-clock
# blow-up). It is a budgeted residual coherence campaign: fix the shared supplier
# first, re-measure the WHOLE-PROJECT oracle, fix only the residual importers it
# still proves broken, bounded by a wall-clock budget + a recheck cap. These
# readers expose the campaign knobs (defaults mirror ``codd/defaults.yaml``).

#: Default cumulative wall-clock for ONE broad CAMPAIGN (across all its phases),
#: in seconds. The campaign stops before an AI phase whose start would leave less
#: than (a min call budget + an oracle recheck reserve), then honest-fails with a
#: partial-progress record. 2700s = 45 min — a generous-but-bounded budget that a
#: residual campaign (1 supplier phase + a few residual phases) fits inside, vs the
#: 4h legacy-broad timeout. Override via ``implement.oracle_broad_wall_clock_seconds``.
DEFAULT_ORACLE_BROAD_WALL_CLOCK_SECONDS = 2700.0
#: Default cap on whole-project oracle RECHECKS inside one campaign (each phase is
#: followed by a recheck). Bounds the campaign independent of the wall-clock so a
#: fast-but-non-converging SUT still terminates. Override via
#: ``implement.oracle_broad_max_rechecks``.
DEFAULT_ORACLE_BROAD_MAX_RECHECKS = 8
#: Reserve (seconds) kept back for the final whole-project oracle recheck + a
#: minimal AI call, so the budget check never starts a phase it cannot also verify.
#: Conservative; the oracle recheck is cheap relative to an AI phase.
_BROAD_MIN_CALL_BUDGET_SECONDS = 60.0
_BROAD_ORACLE_RECHECK_RESERVE_SECONDS = 30.0


def _oracle_broad_strategy(config: Mapping[str, Any] | None) -> str:
    """``implement.oracle_broad_strategy`` → ``incremental`` (default) | ``legacy``.

    ``incremental`` runs the budgeted residual-coherence campaign on a wide-fan-out
    broad. ``legacy`` runs the whole-project regen (the old behaviour). An
    unrecognized value falls back to ``incremental`` (never breaks a build on a
    config typo).
    """
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "oracle_broad_strategy" in section:
        value = str(section["oracle_broad_strategy"]).strip().lower()
        if value in ("incremental", "legacy"):
            return value
    return "incremental"


def _oracle_legacy_broad_enabled(config: Mapping[str, Any] | None) -> bool:
    """``implement.oracle_legacy_broad_enabled`` (default False) OR strategy=legacy.

    True ⇒ a wide-fan-out artifact uses the LEGACY whole-project broad rerun
    instead of the incremental campaign. Either the explicit flag or
    ``oracle_broad_strategy: legacy`` selects legacy.
    """
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and section.get("oracle_legacy_broad_enabled") is True:
        return True
    return _oracle_broad_strategy(config) == "legacy"


def _oracle_broad_wall_clock_seconds(config: Mapping[str, Any] | None) -> float:
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        raw = section.get("oracle_broad_wall_clock_seconds")
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return DEFAULT_ORACLE_BROAD_WALL_CLOCK_SECONDS


def _oracle_broad_max_rechecks(config: Mapping[str, Any] | None) -> int:
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "oracle_broad_max_rechecks" in section:
        try:
            value = int(section["oracle_broad_max_rechecks"])
        except (TypeError, ValueError):
            return DEFAULT_ORACLE_BROAD_MAX_RECHECKS
        return value if value >= 1 else DEFAULT_ORACLE_BROAD_MAX_RECHECKS
    return DEFAULT_ORACLE_BROAD_MAX_RECHECKS


#: Default importer chunk size for the residual phase (owner-tasks per recheck).
DEFAULT_ORACLE_RESIDUAL_CHUNK_SIZE = 2


def _oracle_residual_chunk_size(config: Mapping[str, Any] | None) -> int:
    """``implement.oracle_residual_chunk_size`` — residual importer owners per chunk.

    The residual phase repairs at most this many residual owner-tasks per recheck
    (dependency-ordered), iterating chunk-by-chunk so a large residual stays
    bounded per rerun. <=0 ⇒ the default (never "no limit" by accident).
    """
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "oracle_residual_chunk_size" in section:
        try:
            value = int(section["oracle_residual_chunk_size"])
        except (TypeError, ValueError):
            return DEFAULT_ORACLE_RESIDUAL_CHUNK_SIZE
        return value if value >= 1 else DEFAULT_ORACLE_RESIDUAL_CHUNK_SIZE
    return DEFAULT_ORACLE_RESIDUAL_CHUNK_SIZE


#: Orphan-artifact gate modes. ``warn`` (default) observes + reports orphans but
#: never blocks; ``enforce`` makes an orphan a hard stage failure; ``off`` disables
#: the check. Conservative default because the check is heuristic (see
#: ``find_orphan_artifacts``) — a large behaviour change starts as warn.
_ORPHAN_GATE_MODES = ("off", "warn", "enforce")
DEFAULT_ORPHAN_ARTIFACT_GATE = "warn"


def _orphan_artifact_gate_mode(config: Mapping[str, Any] | None) -> str:
    """``implement.orphan_artifact_gate`` → ``off`` | ``warn`` | ``enforce``.

    Default ``warn`` (observe + report, never block). An unrecognized value falls
    back to the default rather than erroring — the gate must never be the thing
    that breaks a build over a config typo.
    """
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "orphan_artifact_gate" in section:
        raw = section["orphan_artifact_gate"]
        if isinstance(raw, bool):  # tolerate a bool: True→enforce, False→off
            return "enforce" if raw else "off"
        value = str(raw).strip().lower()
        if value in _ORPHAN_GATE_MODES:
            return value
    return DEFAULT_ORPHAN_ARTIFACT_GATE


def _oracle_timeout_seconds(config: Mapping[str, Any] | None) -> float:
    """Bounded wall-clock for the oracle command (``implement.oracle_timeout_seconds``)."""
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        raw = section.get("oracle_timeout_seconds")
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return DEFAULT_ORACLE_TIMEOUT_SECONDS


#: tsc on a fresh build is fast, but a cold first run can compile a large graph;
#: a generous-but-bounded budget. Override via ``implement.oracle_timeout_seconds``.
DEFAULT_ORACLE_TIMEOUT_SECONDS = 600.0

#: Install can pull a tree on a cold cache. Bounded; the verify preflight uses the
#: same magnitude (see ``verify_runner.DEFAULT_INSTALL_TIMEOUT_SECONDS``).
DEFAULT_ORACLE_INSTALL_TIMEOUT_SECONDS = 900.0


# ── contract-aware feedback (exporter surface + targeted-edit directives) ──
#
# The base ``ImplementOracleResult.feedback_message()`` is contract-aware in
# vocabulary (it names the broken edge + the no-invent rule) but cannot see the
# EXPORTER's current interface or the rerun's allowed paths — those need
# ``project_root`` + the derived scope, which only the gate holds. This builder
# folds three convergence levers onto the base message at call time:
#
#   (1) EXPORTER SURFACE  — for each broken edge, the target module's CURRENT
#       public exports ("./cli exports {run}"). Turns "invent a name for the
#       missing symbol" into "reconcile to one of THESE". The #1 anti-oscillation
#       lever (GPT-5.5 Pro consult, 2026-06-15: feedback 3a).
#   (2) TARGETED-EDIT     — on a SCOPED (narrow/expanded) rerun, the explicit
#       "edit only these files, minimal diff, do not create new files/symbols"
#       directive + the allowed-paths fence list. This is what makes a scoped
#       rerun a localized RECONCILE instead of a full re-sample (change 2). Broad
#       reruns omit it (broad legitimately regenerates).
#   (3) it is purely ADDITIVE — a stack with no surface extractor (unknown
#       language) and no scope still gets the full base message, unchanged.
#
# Language-agnostic: the surface extraction lives behind
# ``implement_oracle_scope.extract_public_surface`` (per-language; TS today,
# graceful ``None`` otherwise). No TS-specific text appears here.

#: Cap on exporter-surface entries listed in feedback (bounded prompt).
_FEEDBACK_SURFACE_CAP = 12
#: Cap on individual export names shown per module (a barrel can export hundreds).
_FEEDBACK_SURFACE_NAMES_CAP = 40


def build_contract_feedback(
    result: ImplementOracleResult,
    *,
    project_root: Path,
    scope: Any = None,
) -> str:
    """The SUT-facing feedback for a rerun: base message + surface + edit directives.

    Always includes ``result.feedback_message()`` (the contract + no-invent rule).
    Appends the EXPORTER SURFACE block when an extractor can recover any broken
    edge's target exports, and — when ``scope`` is a NON-broad (scoped) rerun — the
    TARGETED-EDIT block (minimal-diff directive + the allowed-paths write-fence).
    Best-effort: any enrichment failure degrades to the base message (never raises).
    """
    base = result.feedback_message()
    blocks: list[str] = [base]

    # (1) Exporter surface — the current public interface of each broken edge's
    # target module, so the SUT reconciles to a REAL symbol instead of guessing.
    surface_block = _exporter_surface_block(result, project_root)
    if surface_block:
        blocks.append(surface_block)

    # (2) Targeted-edit directive — for any FENCED rerun. A rerun is fenced when it
    # is a scoped (narrow/expanded) scope, OR a broad-CAMPAIGN PHASE scope (which is
    # logically broad — rung=broad — but carries non-empty ``allowed_paths``, so each
    # phase is still a localized, minimal-diff reconcile). Only the LEGACY whole-
    # project broad (scope None, or is_broad() with NO allowed_paths and no plan)
    # legitimately regenerates everything and gets no minimal-diff fence.
    if _is_fenced_scope(scope):
        edit_block = _targeted_edit_block(scope)
        if edit_block:
            blocks.append(edit_block)

    return "\n\n".join(blocks)


def _is_fenced_scope(scope: Any) -> bool:
    """True when ``scope`` runs UNDER a write-fence (scoped OR broad-campaign phase).

    Mirrors the pipeline's fence decision: a scope is fenced when it has non-empty
    ``allowed_paths`` (a narrow/expanded scope, or a broad-campaign phase scope), so
    the targeted-edit / minimal-diff feedback applies. The legacy whole-project broad
    (no ``allowed_paths``, no ``repair_plan``) is NOT fenced.
    """
    if scope is None:
        return False
    has_allowed = bool(getattr(scope, "allowed_paths", ()) or ())
    if has_allowed:
        return True
    # A non-broad scope without an explicit fence still gets the directive (legacy
    # narrow/expanded behaviour — its fence may be supplied elsewhere).
    return not bool(getattr(scope, "is_broad", lambda: True)())


def _exporter_surface_block(result: ImplementOracleResult, project_root: Path) -> str:
    """The 'current public interface of the demanded module(s)' feedback block."""
    if not result.diagnostics:
        return ""
    try:
        from codd.implement_oracle_scope import exporter_surface_for_diagnostics

        surfaces = exporter_surface_for_diagnostics(result.diagnostics, project_root)
    except Exception:  # noqa: BLE001 — surface enrichment is best-effort.
        return ""
    if not surfaces:
        return ""
    lines = [
        "CURRENT PUBLIC INTERFACE of the demanded module(s) — reconcile your "
        "imports to these EXACT exports (do not invent members not listed):",
    ]
    for path, names in list(surfaces.items())[:_FEEDBACK_SURFACE_CAP]:
        if names:
            shown = names[:_FEEDBACK_SURFACE_NAMES_CAP]
            more = len(names) - len(shown)
            suffix = f", … (+{more} more)" if more > 0 else ""
            lines.append(f"  - `{path}` exports: {{{', '.join(shown)}}}{suffix}")
        else:
            lines.append(
                f"  - `{path}` exports NOTHING — the module has no public exports, so "
                f"importing any named symbol from it is wrong (add the export to it, "
                f"or import from the correct module)."
            )
    extra = len(surfaces) - _FEEDBACK_SURFACE_CAP
    if extra > 0:
        lines.append(f"  ... and {extra} more module(s).")
    return "\n".join(lines)


def _targeted_edit_block(scope: Any) -> str:
    """The minimal-diff + write-fence feedback block for a SCOPED rerun.

    Tells the SUT to RECONCILE the named files with the smallest possible change
    and forbids creating files outside the scope — the prompt-side half of the
    write-fence (the pipeline enforces the fence by reverting out-of-scope writes).
    """
    allowed = tuple(getattr(scope, "allowed_paths", ()) or ())
    rung = getattr(scope, "rung", "scoped")
    lines = [
        f"TARGETED EDIT ({rung} scope): this is a LOCALIZED repair, not a "
        "regeneration. Make the SMALLEST edit that makes the typecheck pass — "
        "reconcile the imports/exports between the files below. Do NOT regenerate "
        "them from scratch, do NOT create new files, and do NOT add new public "
        "symbols beyond what is needed to satisfy the existing imports.",
    ]
    if allowed:
        shown = list(allowed)[:_FEEDBACK_SURFACE_CAP]
        more = len(allowed) - len(shown)
        suffix = f", … (+{more} more)" if more > 0 else ""
        lines.append(
            "You may ONLY create/modify these paths (anything else you write will "
            f"be reverted): {', '.join(shown)}{suffix}."
        )
    return "\n".join(lines)


# ── scope certification (anti-false-green: the oracle must SEE src + tests) ──


def _norm(rel: str) -> str:
    return str(rel).strip().replace("\\", "/").strip("/")


def _glob_covers_root(patterns: list[str], root: str) -> bool:
    """Does any tsconfig include/files glob cover everything under ``root``/?

    Conservative TEXTUAL test (we do not run tsc's resolver): a pattern covers
    ``root`` when it starts at (or above) ``root`` and is recursive enough to
    reach nested files — i.e. ``root`` itself, ``root/**``, ``root/**/*`` and
    ``**/*`` (the catch-all). A pattern restricted to a sub-path of ``root`` (a
    single file, or ``root/sub/**``) does NOT certify the whole ``root`` (e2e /
    helpers under another sub-dir would be unseen). Anything we cannot prove
    covers ``root`` returns False → the caller HARD-FAILS rather than guessing.
    """
    root = _norm(root)
    if not root:
        return False
    for raw in patterns:
        pat = _norm(raw)
        if not pat:
            continue
        # The universal recursive catch-all.
        if pat in {"**", "**/*"} or pat.startswith("**/"):
            # ``**/*`` and ``**/<x>`` reach into every dir incl. root.
            if pat in {"**", "**/*"}:
                return True
            # ``**/*.ts`` etc. — recursive over all dirs, covers root's files.
            if pat.startswith("**/*"):
                return True
        # ``root`` exactly, or a recursive glob anchored at root.
        if pat == root:
            return True
        if pat.startswith(root + "/"):
            tail = pat[len(root) + 1 :]
            # Recursive from root: ``root/**`` / ``root/**/*`` / ``root/**/*.ts``.
            if tail.startswith("**"):
                return True
            # A single-level ``root/*`` does NOT reach nested e2e/helpers; only a
            # ``**`` recursive glob certifies the whole subtree.
    return False


def certify_oracle_scope(
    project_root: Path,
    profile: LayoutProfile,
    spec: ImplementOracleSpec,
) -> str:
    """Certify the oracle's config covers source + tests, else raise OracleScopeError.

    TS today: parse ``tsconfig.json`` and prove its ``include`` (or ``files``)
    covers the ``source_root`` and ``test_root`` subtrees (which contain e2e +
    helpers). Returns a human-readable certification detail on success.

    A missing/unparseable tsconfig, or one whose scope provably excludes the test
    tree, is a HARD FAIL (``OracleScopeError``) — a green oracle over an unknown
    or partial scope would be a false-green. This is the #1 design failure mode
    ("profile-less native oracle: tests outside compile scope = false green").

    Python (``spec.kind == "composite"``) certifies a CONCRETE file-list instead
    of a config glob: the oracle itself enumerates every ``.py`` it will hand each
    tool, so the certification proves the required root(s) contain at least one
    ``.py`` (an empty required root is a HARD FAIL — a green oracle over an empty
    scope proves nothing). See :func:`certify_python_oracle_scope`.
    """
    if profile.language == "python" and spec.kind == "composite":
        return certify_python_oracle_scope(project_root, profile, spec)
    if profile.language == "go" and spec.kind == "composite":
        return certify_go_oracle_scope(project_root, profile, spec)
    if profile.language not in ("typescript", "node"):
        # Only TS has a config-scope to certify today; other compiler stacks add
        # their own certifier when they wire an oracle. (Never reached for the
        # current registry — the resolver only hands us TS / the composites above.)
        return f"scope certification not implemented for {profile.language!r}; relying on command"

    tsconfig = project_root / "tsconfig.json"
    if not tsconfig.is_file():
        raise OracleScopeError(
            "implement-time oracle cannot be certified: no tsconfig.json at the project "
            "root, so tsc's typecheck scope is unknown — a green typecheck would prove "
            "nothing about src/tests. The greenfield scaffold creates a tsconfig with "
            "include=[<src>/**/*, <tests>/**/*]; ensure the layout was scaffolded."
        )
    try:
        payload = json.loads(_strip_jsonc(tsconfig.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise OracleScopeError(
            f"implement-time oracle cannot be certified: tsconfig.json is unreadable/"
            f"invalid JSON ({exc}); tsc's scope is undecidable, so a green typecheck "
            f"cannot be trusted to cover src/tests."
        ) from exc

    if not isinstance(payload, dict):
        raise OracleScopeError(
            "implement-time oracle cannot be certified: tsconfig.json is not a JSON object."
        )

    include = payload.get("include")
    files = payload.get("files")
    patterns: list[str] = []
    if isinstance(include, list):
        patterns.extend(str(item) for item in include)
    if isinstance(files, list):
        patterns.extend(str(item) for item in files)

    # A tsconfig with neither ``include`` nor ``files`` defaults to "every .ts
    # under the config dir" — which DOES cover src + tests. But ``exclude`` or a
    # narrow project layout could still hide the test tree; rather than reason
    # about tsc's full default-resolution, we REQUIRE an explicit include that we
    # can prove covers the test root. This is intentionally strict: the scaffold
    # always emits one, so a missing include means a hand-authored config we will
    # not certify blind.
    if not patterns:
        raise OracleScopeError(
            "implement-time oracle cannot be certified: tsconfig.json declares no "
            "`include` or `files`, so it is not provable that tsc's scope covers the "
            "test tree (where test/helper symbol incoherence lives). Declare "
            f'include: ["{profile.source_root}/**/*", "{profile.test_root}/**/*"].'
        )

    missing: list[str] = []
    if spec.scope.require_source_root and not _glob_covers_root(patterns, profile.source_root):
        missing.append(profile.source_root)
    if spec.scope.require_test_root and not _glob_covers_root(patterns, profile.test_root):
        missing.append(profile.test_root)
    if missing:
        raise OracleScopeError(
            "implement-time oracle cannot be certified: tsconfig.json `include`/`files` "
            f"({patterns}) does not provably cover the required root(s) {missing}. The "
            "whole point of the implement-time typecheck is to catch test/helper symbol "
            "incoherence, so an uncovered test tree is a HARD FAIL (anti-false-green). "
            f'Add a recursive glob, e.g. "{missing[0]}/**/*", to include.'
        )

    return (
        f"oracle scope certified: tsconfig include/files {patterns} cover "
        f"source_root='{profile.source_root}' + test_root='{profile.test_root}'"
    )


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` and ``/* */`` comments so a JSONC tsconfig parses as JSON.

    Conservative: removes line comments not inside a string and block comments.
    tsconfig is JSONC; our own scaffold uses a ``"//"`` KEY (valid JSON) but a
    hand-authored config may use real comments, and JSON's parser would choke.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    in_line_comment = False
    in_block_comment = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            out.append(ch)
            if ch == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        # not in string/comment
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ── evidence normalization (tsc diagnostics → language-neutral categories) ──


def _categorize_ts_code(code: str) -> str:
    if code in _TS_MISSING_SYMBOL_CODES:
        return EVIDENCE_MISSING_SYMBOL
    if code in _TS_MODULE_RESOLUTION_CODES:
        return EVIDENCE_MODULE_RESOLUTION
    return EVIDENCE_OTHER


def normalize_oracle_output(
    output: str,
    *,
    command: str,
    project_root: Path,
    profile: LayoutProfile,
) -> tuple[list[ImplementOracleFinding], list[str]]:
    """Normalize raw oracle output → (findings, editable failed paths).

    For TS this parses every ``error TSxxxx: message`` line into an
    :class:`ImplementOracleFinding` with a language-neutral category, and REUSES
    the existing tsc attribution
    (:func:`codd.repair.test_failure_attribution.attribute_command_failure`) to
    resolve the editable source/test targets — the same parser the verify stage
    uses, so attribution stays consistent across stages.

    A ``No inputs were found`` (TS18003) result is surfaced as a single
    ``environment_build_error`` finding: tsc ran but typechecked nothing, which
    must never be mistaken for coherence (the scope certifier should have caught
    it first; this is the belt-and-suspenders).

    Python dispatches to :func:`normalize_python_tool_output` (the ``pytest
    --collect-only`` parser) — the composite oracle's compile + import-resolver
    layers build their findings in-process (no text to normalize), so the only
    text surface a Python oracle normalizes is the pytest collect output.
    """
    if profile.language == "python":
        return normalize_python_tool_output(
            output, command=command, project_root=project_root, profile=profile
        )
    text = output or ""
    findings: list[ImplementOracleFinding] = []

    if _TS_NO_INPUTS_RE.search(text):
        findings.append(
            ImplementOracleFinding(
                category=EVIDENCE_ENVIRONMENT_BUILD,
                code="TS18003",
                message="tsc found no inputs — the typecheck covered zero files (scope error).",
            )
        )

    # Attribute diagnostic lines to their files using the same regexes tsc uses,
    # pairing each error code/message with the file on the SAME line.
    for line in text.splitlines():
        m = _TS_DIAG_LINE.search(line)
        if m is None:
            continue
        code = m.group("code")
        message = m.group("message").strip()
        path = _diag_path(line, project_root)
        findings.append(
            ImplementOracleFinding(
                category=_categorize_ts_code(code),
                code=code,
                message=message,
                path=path,
            )
        )

    failed_paths: list[str] = []
    try:
        from codd.repair.test_failure_attribution import attribute_command_failure

        attribution = attribute_command_failure(
            command=command,
            output=text,
            project_root=project_root,
            check_name="implement_oracle",
        )
        if attribution is not None:
            failed_paths = list(attribution.failed_nodes)
    except Exception:  # noqa: BLE001 — attribution is best-effort enrichment.
        failed_paths = []

    return findings, failed_paths


def _diag_path(line: str, project_root: Path) -> str | None:
    """Extract the file path from a tsc diagnostic line, project-relative."""
    paren = re.match(r"^\s*(?P<path>[^\s(][^(\n]*\.(?:ts|tsx|mts|cts))\(\d+,\d+\)", line)
    colon = re.match(r"^\s*(?P<path>[^\s:][^:\n]*\.(?:ts|tsx|mts|cts)):\d+:\d+", line)
    match = paren or colon
    if match is None:
        return None
    raw = match.group("path").strip()
    try:
        resolved = (project_root / raw).resolve()
        return resolved.relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(raw.replace("\\", "/")).as_posix()


# ═════════════════════════════════════════════════════════════════════════════
# Python composite implement-oracle (the no-single-compiler stack's equivalent of
# tsc --noEmit). Design: /tmp/gpt_python_oracle.txt (GPT-5.5 Pro, 2026-06-17).
#
# THREE HARD LAYERS (py_compile + collect-only ALONE are insufficient — neither
# resolves a first-party import a test never touches: the keystone false-green):
#   1. compile          — in-process compile() over ALL source+test .py
#                         (SyntaxError/IndentationError/TabError/UnicodeDecodeError).
#   2. first-party imports (THE CORE) — an AST visitor over ALL source+test .py
#                         that resolves every first-party module + imported symbol
#                         against a module index built from the profile's package.
#   3. pytest collect   — `python -m pytest <test_root> --collect-only -q` for
#                         test-surface importability.
#
# FALSE-RED AVOIDANCE (these PASS / are ignored, NEVER a hard fail):
#   * `if TYPE_CHECKING:` block imports (a runtime oracle ignores type-only edges).
#   * `try: import X except ImportError:` guarded imports (optional by intent).
#   * third-party imports (not in the first-party index — only first-party hard).
#   * non-literal dynamic imports (`importlib.import_module(var)`); only a LITERAL
#     `importlib.import_module("app.x")` / `__import__("app.x")` is checked.
#   * a module whose provided-name set is undecidable (dynamic `__all__`, an
#     unresolved `import *`) → symbol provider UNKNOWN → no symbol fail.
# Policy mirrors codd/test_import_coherence.py: PROVABLY absent → fail; unknown →
# never fail.
# ═════════════════════════════════════════════════════════════════════════════

#: Directories never enumerated by the Python oracle (caches, vcs, venvs, builds).
_PY_ORACLE_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".codd",
        ".pytest_cache",
        ".venv",
        "venv",
        "env",
        "build",
        "dist",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        "node_modules",
    }
)

#: A module whose provided-name set cannot be statically decided (dynamic
#: ``__all__`` / unresolved ``import *`` / unreadable). Importing any symbol from
#: such a module is NEVER flagged (anti-false-RED) — mirrors test_import_coherence.
_PY_PROVIDES_UNKNOWN = object()

#: ``implement.python_name_lint`` modes. ``optional`` (default) runs ruff/pyflakes
#: if present, else SKIPS (no undefined-local-name claim); ``required`` makes its
#: absence an environment_build_error; ``off`` never runs it. Lint is a SEPARATE
#: registry contract (``python.undefined_name_lint.v1``) — when it is skipped the
#: composite oracle does NOT claim undefined-local-name coverage.
_PY_LINT_MODES = ("off", "optional", "required")
DEFAULT_PYTHON_NAME_LINT = "optional"

#: Bounded wall-clock for ``pytest --collect-only`` (cold collect of a large
#: suite). Override via ``implement.python_collect_timeout_seconds``.
DEFAULT_PYTHON_COLLECT_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class PythonOracleScope:
    """The concrete ``.py`` file-list a Python composite oracle is certified over.

    ``source_files`` / ``test_files`` are project-relative POSIX paths; the
    executor re-derives the SAME enumeration and asserts each required in-process
    tool OBSERVED all of them (the observability gate — anti-false-green).
    """

    source_files: tuple[str, ...]
    test_files: tuple[str, ...]

    @property
    def expected_files(self) -> tuple[str, ...]:
        """All in-scope files, deduped, source-then-test order preserved."""
        return tuple(dict.fromkeys(self.source_files + self.test_files))


@dataclass(frozen=True)
class PythonToolRun:
    """One Python oracle tool's result + its observation trail (for the gate)."""

    name: str
    executed: bool
    observed_files: tuple[str, ...] = ()
    findings: tuple[ImplementOracleFinding, ...] = ()
    output: str = ""
    optional: bool = False
    skipped_reason: str = ""


@dataclass(frozen=True)
class _PyImportDemand:
    """One runtime import edge an AST visitor found (module + optional symbol).

    ``level`` > 0 is a relative import; ``module`` is the dotted target (may be
    ``None`` for ``from . import name``); ``symbol`` is the imported name for the
    symbol-existence check (``None`` for a plain ``import a.b`` — module-only).
    ``guarded`` marks a ``try/except ImportError`` import (never hard-failed).
    """

    module: str | None
    level: int
    symbol: str | None
    lineno: int
    guarded: bool = False


def _py_norm(rel: str) -> str:
    return str(rel).strip().replace("\\", "/").strip("/")


def _py_rel_project(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(str(path).replace("\\", "/")).as_posix()


def _iter_python_oracle_files(project_root: Path, root_rel: str) -> tuple[str, ...]:
    """Every ``.py`` under ``root_rel`` (project-relative), skip-dirs excluded."""
    rel = _py_norm(root_rel)
    if not rel:
        return ()
    root = project_root / rel
    if not root.is_dir():
        return ()
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in _PY_ORACLE_SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        out.append(_py_rel_project(path, project_root))
    return tuple(out)


def _python_oracle_scope(
    project_root: Path, profile: LayoutProfile, spec: ImplementOracleSpec
) -> PythonOracleScope:
    """Enumerate the source+test ``.py`` the oracle will check (deduped)."""
    source_files = _iter_python_oracle_files(project_root, profile.source_root)
    test_root_files = _iter_python_oracle_files(project_root, profile.test_root)
    source_set = set(source_files)
    # test_root may nest under source_root in odd layouts — keep test files that are
    # not already counted as source so the same file is observed under one bucket.
    test_files = tuple(f for f in test_root_files if f not in source_set)
    return PythonOracleScope(source_files=source_files, test_files=test_files)


def certify_python_oracle_scope(
    project_root: Path,
    profile: LayoutProfile,
    spec: ImplementOracleSpec,
) -> str:
    """Certify the Python oracle's CONCRETE file-list covers the required root(s).

    A required root (per ``spec.scope.require_source_root`` /
    ``require_test_root``) with ZERO ``.py`` is a HARD FAIL
    (:class:`OracleScopeError`) — a green oracle over an empty scope proves
    nothing (the #1 anti-false-green failure mode). Returns a human-readable
    detail on success. (The executor re-enumerates + asserts each tool observed
    the whole list; this is the front gate.)
    """
    scope = _python_oracle_scope(project_root, profile, spec)
    missing_roots: list[str] = []
    if spec.scope.require_source_root and not scope.source_files:
        missing_roots.append(profile.source_root)
    if spec.scope.require_test_root and not scope.test_files:
        missing_roots.append(profile.test_root)
    if missing_roots:
        raise OracleScopeError(
            "python implement-time oracle cannot be certified: no .py files observed "
            f"under required root(s) {missing_roots}. A green oracle over an empty "
            "scope proves nothing — the whole point of the implement-time oracle is "
            "to check the generated source AND tests, so an empty required root is a "
            "HARD FAIL (anti-false-green). Ensure the layout was scaffolded and the "
            "units were generated."
        )
    return (
        "python oracle scope certified: "
        f"{len(scope.source_files)} source .py + {len(scope.test_files)} test .py "
        f"observed under source_root='{profile.source_root}' + "
        f"test_root='{profile.test_root}'"
    )


def certify_go_oracle_scope(
    project_root: Path,
    profile: LayoutProfile,
    spec: ImplementOracleSpec,  # noqa: ARG001 — signature parity with the TS/Python certifiers.
) -> str:
    """Certify the Go composite's module covers buildable source, else hard-fail.

    Go's oracle is ``go build ./...`` + ``go vet ./...`` over the WHOLE module —
    there is no config glob to certify (``./...`` is itself the whole-module
    scope). The anti-false-green concern is instead an EMPTY module: a green ``go
    build`` over a module with NO ``.go`` files proves nothing. So we certify (a)
    a ``go.mod`` exists at the module root (without it ``go build ./...`` is not a
    module build at all) and (b) at least one ``.go`` file exists under it. A
    missing go.mod or a ``.go``-less module is a HARD FAIL
    (:class:`OracleScopeError`) — mirroring the Python certifier's "empty required
    root is a hard fail". (Colocated ``*_test.go`` builds/vets WITH source under
    ``./...``, so there is no separate test-root glob to certify — hence the
    synthesized spec sets ``require_test_root=False``.)
    """
    module_root = project_root / _go_norm_module_root(profile)
    gomod = module_root / "go.mod"
    if not gomod.is_file():
        raise OracleScopeError(
            "go implement-time oracle cannot be certified: no go.mod at the module "
            f"root ({_go_norm_module_root(profile)!r}), so `go build ./...` is not a "
            "module build and a green result would prove nothing. Ensure the Go "
            "module was scaffolded (go.mod present)."
        )
    has_go_file = False
    if module_root.is_dir():
        for path in module_root.rglob("*.go"):
            parts = set(path.parts)
            if ".git" in parts or "vendor" in parts:
                continue
            if path.is_file():
                has_go_file = True
                break
    if not has_go_file:
        raise OracleScopeError(
            "go implement-time oracle cannot be certified: no .go files under the "
            f"module root ({_go_norm_module_root(profile)!r}). A green `go build "
            "./...` over an empty module proves nothing — an empty scope is a HARD "
            "FAIL (anti-false-green). Ensure the units were generated."
        )
    return (
        "go oracle scope certified: go.mod present + ≥1 .go file under module_root="
        f"'{_go_norm_module_root(profile)}' (go build ./... + go vet ./... cover the whole module)"
    )


# ── first-party module index (built from the profile's package) ──────────────


@dataclass
class _PyModuleInfo:
    """An indexed first-party module/package + its lazily-computed provided names."""

    rel: str
    is_package: bool  # an __init__.py (a package) vs a plain module
    tree: ast.AST | None


class _PyFirstPartyIndex:
    """First-party dotted-module → file index, with statically-provided symbols.

    First-party == under the profile's ``package_root`` (addressable as
    ``<package_name>.<...>``) PLUS any module under ``source_root`` that is not
    under ``package_root`` (a flat ``src/foo.py`` → ``foo``). Test-tree modules
    are indexed too (so a test importing a sibling test/helper module is checked).
    A dotted key resolves to a module file OR (for a package key) its ``__init__``.
    """

    def __init__(self) -> None:
        self.modules: dict[str, _PyModuleInfo] = {}
        #: dotted keys that are PACKAGES (have a dir, with or without __init__).
        self.packages: set[str] = set()
        #: project-rel path → its first-party dotted module key (the anchor for
        #: resolving that file's RELATIVE imports; computed at index-build time so
        #: the namespace root matches the index keys, not the raw project path).
        self.rel_to_key: dict[str, str] = {}
        self._provided_cache: dict[str, Any] = {}
        self._module_getattr_cache: dict[str, bool] = {}

    def register(self, key: str, info: _PyModuleInfo) -> None:
        self.modules.setdefault(key, info)
        self.rel_to_key.setdefault(_py_norm(info.rel), key)

    def dotted_key_for_rel(self, rel: str) -> str | None:
        """The first-party dotted module key for an indexed file, or ``None``."""
        return self.rel_to_key.get(_py_norm(rel))

    def has_module(self, key: str) -> bool:
        return key in self.modules

    def has_package(self, key: str) -> bool:
        return key in self.packages or key in self.modules

    def resolves(self, key: str) -> bool:
        """A dotted key resolves when it is a known module OR a known package."""
        return self.has_module(key) or self.has_package(key)

    def has_module_getattr(self, key: str) -> bool:
        """True when the module file declares a MODULE-LEVEL ``__getattr__`` (PEP 562).

        A module-level ``def __getattr__(name)`` provides attributes dynamically, so
        ``from mod import X`` for such a module is statically UNDECIDABLE — flagging
        X missing would be a false-RED. Kept SEPARATE from :meth:`provided_names`
        (a star/re-export of such a module must NOT become UNKNOWN — that would widen
        the false-GREEN surface); only a direct named import from the bearer is
        excused, at the call site. Module-level only (a nested / class ``__getattr__``
        is not PEP 562). ``__dir__`` does NOT count (it controls ``dir()`` display,
        not attribute lookup).
        """
        cached = self._module_getattr_cache.get(key)
        if cached is not None:
            return cached
        info = self.modules.get(key)
        result = False
        if info is not None and info.tree is not None:
            result = any(
                isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
                for node in _module_level_statements(info.tree)
            )
        self._module_getattr_cache[key] = result
        return result

    def is_first_party_prefix(self, key: str) -> bool:
        """True when ``key`` is, or is under, a known first-party top-level name."""
        head = key.split(".", 1)[0]
        if head in self._roots:
            return True
        return False

    _roots: frozenset[str] = frozenset()

    def provided_names(self, key: str, _stack: tuple[str, ...] = ()) -> Any:
        """Names a module provides (set[str]) or ``_PY_PROVIDES_UNKNOWN``.

        Resolves top-level ``def``/``class``/assignments + module-top imported
        names (a re-export) + a static ``__all__`` + ``from Y import *`` where Y
        is another first-party module (bounded recursion). A dynamic ``__all__``
        or an unresolved star makes the module UNKNOWN (never flagged).
        """
        if key in self._provided_cache:
            return self._provided_cache[key]
        if key in _stack:  # import cycle — bail conservatively
            return _PY_PROVIDES_UNKNOWN
        info = self.modules.get(key)
        if info is None or info.tree is None:
            return _PY_PROVIDES_UNKNOWN
        result = self._compute_provided(info, _stack + (key,))
        self._provided_cache[key] = result
        return result

    def _compute_provided(self, info: _PyModuleInfo, stack: tuple[str, ...]) -> Any:
        names: set[str] = set()
        star_unknown = False
        dunder_all: list[str] | None = None
        dunder_all_dynamic = False
        # Walk ALL module-LEVEL statements — including names bound inside top-level
        # ``try``/``if``/``with``/``for``/``while`` blocks (a conditional/guarded
        # definition is still a real module attribute, e.g.
        # ``try: from .optional import x except ImportError: x = None`` provides
        # ``x``). Function/class BODIES are NOT descended (those are local scopes).
        for node in _module_level_statements(info.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    self._add_assign_target(target, names)
                if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                    extracted = _py_string_list(node.value)
                    if extracted is None:
                        dunder_all_dynamic = True
                    else:
                        dunder_all = (dunder_all or []) + extracted
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    names.add(node.target.id)
                    if node.target.id == "__all__":
                        extracted = _py_string_list(node.value) if node.value else None
                        if extracted is None:
                            dunder_all_dynamic = True
                        else:
                            dunder_all = (dunder_all or []) + extracted
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    if bound:
                        names.add(bound)
            elif isinstance(node, ast.ImportFrom):
                if any(a.name == "*" for a in node.names):
                    sub = self._resolve_star_source(info, node, stack)
                    if sub is _PY_PROVIDES_UNKNOWN:
                        star_unknown = True
                    else:
                        names |= sub  # type: ignore[arg-type]
                else:
                    for alias in node.names:
                        bound = alias.asname or alias.name
                        if bound:
                            names.add(bound)
        if dunder_all_dynamic:
            return _PY_PROVIDES_UNKNOWN
        if star_unknown:
            return _PY_PROVIDES_UNKNOWN
        if dunder_all is not None:
            return names | set(dunder_all)
        return names

    @staticmethod
    def _add_assign_target(target: ast.AST, names: set[str]) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _PyFirstPartyIndex._add_assign_target(elt, names)

    def _resolve_star_source(self, info: _PyModuleInfo, node: ast.ImportFrom, stack: tuple[str, ...]) -> Any:
        importer_key = self.dotted_key_for_rel(info.rel)
        target_key = self.resolve_import_target_key(
            importer_key=importer_key,
            importer_is_package=info.is_package,
            module=node.module,
            level=node.level,
        )
        if target_key is None or not self.has_module(target_key):
            return _PY_PROVIDES_UNKNOWN
        return self.provided_names(target_key, stack)

    def resolve_import_target_key(
        self,
        *,
        importer_key: str | None,
        importer_is_package: bool,
        module: str | None,
        level: int,
    ) -> str | None:
        """Resolve a (possibly relative) import target to a first-party dotted key.

        Absolute (``level == 0``) → the dotted ``module`` itself. Relative
        (``level > 0``) → resolved against the IMPORTER's OWN dotted key (the
        first-party namespace anchor, NOT the raw project path): ``level`` 1 = the
        importer's package, each extra level climbs one parent. A module's own key
        ends at its leaf; its package is that key minus the leaf (a package
        ``__init__`` is already its package key).
        """
        if level and level > 0:
            if importer_key is None:
                return None  # importer not indexed → cannot anchor a relative import
            base_parts = importer_key.split(".")
            pkg_parts = base_parts if importer_is_package else base_parts[:-1]
            climb = level - 1
            if climb > len(pkg_parts):
                return None
            anchor = pkg_parts[: len(pkg_parts) - climb] if climb else pkg_parts
            suffix = module.split(".") if module else []
            segments = [s for s in (*anchor, *suffix) if s]
            if not segments:
                return None
            return ".".join(segments)
        if module:
            return module
        return None


def _module_level_statements(tree: ast.AST) -> list[ast.stmt]:
    """Flatten module-level statements, descending into compound bodies.

    Yields every statement at MODULE scope — including those nested inside
    top-level ``if`` / ``try`` / ``with`` / ``for`` / ``while`` blocks (their
    bodies/else/handlers/finally are still module scope, so a name bound there is
    a real module attribute). Does NOT descend into ``def`` / ``class`` bodies
    (those open a new, local scope whose names are NOT module attributes).
    """
    out: list[ast.stmt] = []
    body = getattr(tree, "body", [])

    def _walk(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            out.append(stmt)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # new scope — its names are not module attributes
            if isinstance(stmt, ast.If):
                _walk(stmt.body)
                _walk(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                _walk(stmt.body)
                for handler in stmt.handlers:
                    _walk(handler.body)
                _walk(stmt.orelse)
                _walk(stmt.finalbody)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                _walk(stmt.body)
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                _walk(stmt.body)
                _walk(stmt.orelse)
            elif isinstance(stmt, ast.While):
                _walk(stmt.body)
                _walk(stmt.orelse)

    _walk(list(body))
    return out


def _py_module_dotted_parts(rel: str) -> list[str]:
    """Dotted segments for a module rel-path (relative to the FIRST-PARTY root).

    ``app/sub/io.py`` → ``["app", "sub", "io"]``; ``app/__init__.py`` →
    ``["app"]``; the dotted key namespace is rooted at the indexed top-level name.
    """
    parts = list(PurePosixPath(rel).parts)
    if not parts:
        return []
    if parts[-1] == "__init__.py":
        return [p for p in parts[:-1] if p]
    leaf = parts[-1]
    leaf = leaf[:-3] if leaf.endswith(".py") else leaf
    return [p for p in (*parts[:-1], leaf) if p]


def _py_string_list(value: ast.AST | None) -> list[str] | None:
    if not isinstance(value, (ast.List, ast.Tuple)):
        return None
    out: list[str] = []
    for elt in value.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            out.append(elt.value)
        else:
            return None
    return out


def _build_python_module_index(
    project_root: Path, profile: LayoutProfile, scope: PythonOracleScope
) -> _PyFirstPartyIndex:
    """Build the first-party dotted-module index from the profile + the file list.

    The dotted NAMESPACE is rooted so that:
      * a module under ``package_root`` (``src/<pkg>/mod.py``) is keyed
        ``<pkg>.mod`` (the package-absolute name the runtime + tests use);
      * a flat module under ``source_root`` but outside ``package_root``
        (``src/foo.py``) is keyed ``foo``;
      * a test-tree module (``tests/helpers/io.py``) is keyed by its path
        relative to ``test_root`` (``helpers.io``) AND the bare leaf — so a test's
        ``from helpers import io`` / sibling import resolves.
    Every intermediate package dir is registered so ``import <pkg>.sub`` resolves.
    """
    index = _PyFirstPartyIndex()
    roots: set[str] = set()
    source_root = _py_norm(profile.source_root)
    package_root = _py_norm(profile.package_root)
    test_root = _py_norm(profile.test_root)

    def _register_module(dotted_parts: list[str], rel: str) -> None:
        if not dotted_parts:
            return
        roots.add(dotted_parts[0])
        is_pkg = rel.endswith("__init__.py")
        info = _PyModuleInfo(rel=rel, is_package=is_pkg, tree=_py_parse_file(project_root / rel))
        key = ".".join(dotted_parts)
        index.register(key, info)
        # Register every ancestor dir as a package key (so ``import a.b`` where b is
        # a subpackage resolves even when a/__init__.py is the registered module).
        for i in range(1, len(dotted_parts)):
            index.packages.add(".".join(dotted_parts[:i]))
        if is_pkg:
            index.packages.add(key)

    for rel in scope.source_files:
        rel_n = _py_norm(rel)
        if package_root and (rel_n == package_root or rel_n.startswith(package_root + "/")):
            # Under the named package: dotted name rooted at the package dir's PARENT
            # so the package itself becomes the head segment (``<pkg>.mod``).
            inside = rel_n[len(_py_norm(source_root)) + 1 :] if source_root and rel_n.startswith(source_root + "/") else rel_n
            parts = _py_module_dotted_parts(inside)
            _register_module(parts, rel_n)
        elif source_root and rel_n.startswith(source_root + "/"):
            # Flat module under source_root but outside package_root → bare name.
            inside = rel_n[len(source_root) + 1 :]
            parts = _py_module_dotted_parts(inside)
            _register_module(parts, rel_n)
        else:
            # source_root is "" or the file is at project root — key by its own path.
            parts = _py_module_dotted_parts(rel_n)
            _register_module(parts, rel_n)

    for rel in scope.test_files:
        rel_n = _py_norm(rel)
        if test_root and rel_n.startswith(test_root + "/"):
            inside = rel_n[len(test_root) + 1 :]
        else:
            inside = rel_n
        parts = _py_module_dotted_parts(inside)
        _register_module(parts, rel_n)

    index._roots = frozenset(roots)
    return index


def _py_parse_file(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return None


# ── layer 1: in-process compile (syntax / indentation / encoding) ────────────


def _run_python_compile_layer(project_root: Path, files: tuple[str, ...]) -> PythonToolRun:
    """Compile every ``.py`` in-process; emit a finding per syntax/encoding error.

    ``compile(text, path, "exec")`` does NOT resolve imports — its job is to catch
    the syntax class (SyntaxError/IndentationError/TabError) + read/decode errors.
    A SyntaxError is a real coherence error (``EVIDENCE_OTHER``); a decode/read
    error is an environment problem (``EVIDENCE_ENVIRONMENT_BUILD``).
    """
    findings: list[ImplementOracleFinding] = []
    observed: list[str] = []
    for rel in files:
        observed.append(rel)
        path = project_root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="decode_error",
                    message=f"could not decode as UTF-8: {exc}",
                    path=rel,
                )
            )
            continue
        except OSError as exc:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="read_error",
                    message=f"could not read file: {exc}",
                    path=rel,
                )
            )
            continue
        try:
            compile(text, str(path), "exec", dont_inherit=True)
        except SyntaxError as exc:  # IndentationError/TabError are SyntaxError subclasses
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_OTHER,
                    code=type(exc).__name__,
                    message=(exc.msg or str(exc)),
                    path=rel,
                )
            )
        except ValueError as exc:
            # e.g. source containing a NUL byte — a real, honest syntax/source error.
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_OTHER,
                    code="compile_error",
                    message=str(exc),
                    path=rel,
                )
            )
    return PythonToolRun(
        name="python_compile",
        executed=True,
        observed_files=tuple(observed),
        findings=tuple(findings),
    )


# ── layer 2: first-party import resolver (THE CORE) ──────────────────────────


def _iter_runtime_import_demands(tree: ast.AST) -> list[_PyImportDemand]:
    """Collect runtime import edges; SKIP ``TYPE_CHECKING`` blocks; MARK guarded.

    * ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` bodies are NOT walked
      (a runtime oracle ignores type-only imports).
    * imports inside a ``try`` whose handlers catch ``ImportError`` /
      ``ModuleNotFoundError`` are marked ``guarded`` (optional by intent).
    * literal ``importlib.import_module("a.b")`` / ``__import__("a.b")`` become a
      module-only demand; a non-literal arg is ignored (unknown).
    """
    demands: list[_PyImportDemand] = []

    def _is_type_checking_test(test: ast.expr) -> bool:
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING"
        return False

    def _handler_catches_importerror(handlers: list[ast.excepthandler]) -> bool:
        for h in handlers:
            etype = h.type
            if etype is None:
                return True  # bare ``except:`` — swallows ImportError too
            candidates = etype.elts if isinstance(etype, ast.Tuple) else [etype]
            for c in candidates:
                name = c.id if isinstance(c, ast.Name) else (c.attr if isinstance(c, ast.Attribute) else "")
                if name in ("ImportError", "ModuleNotFoundError", "Exception", "BaseException"):
                    return True
        return False

    def _visit(node: ast.AST, *, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking_test(child.test):
                # Skip the TYPE_CHECKING body; still walk the ELSE branch (runtime).
                for sub in child.orelse:
                    _visit(sub, guarded=guarded)
                continue
            if isinstance(child, ast.Try):
                body_guarded = guarded or _handler_catches_importerror(child.handlers)
                for sub in child.body:
                    _visit(sub, guarded=body_guarded)
                for sub in (*child.handlers, *child.orelse, *child.finalbody):
                    _visit(sub, guarded=guarded)
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    demands.append(
                        _PyImportDemand(
                            module=alias.name,
                            level=0,
                            symbol=None,
                            lineno=child.lineno,
                            guarded=guarded,
                        )
                    )
            elif isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.name == "*":
                        # star import — symbol set is the source's; module-only check.
                        demands.append(
                            _PyImportDemand(
                                module=child.module,
                                level=child.level or 0,
                                symbol=None,
                                lineno=child.lineno,
                                guarded=guarded,
                            )
                        )
                    else:
                        demands.append(
                            _PyImportDemand(
                                module=child.module,
                                level=child.level or 0,
                                symbol=alias.name,
                                lineno=child.lineno,
                                guarded=guarded,
                            )
                        )
            elif isinstance(child, ast.Call):
                dynamic = _dynamic_import_demand(child, guarded=guarded)
                if dynamic is not None:
                    demands.append(dynamic)
                _visit(child, guarded=guarded)
            else:
                _visit(child, guarded=guarded)

    _visit(tree, guarded=False)
    return demands


def _dynamic_import_demand(call: ast.Call, *, guarded: bool) -> _PyImportDemand | None:
    """A LITERAL ``importlib.import_module("a.b")`` / ``__import__("a.b")`` demand.

    Only a single string-literal first argument is checked; any non-literal arg
    (a variable / f-string / concat) is unknown → ``None`` (never flagged).
    """
    func = call.func
    is_import_module = isinstance(func, ast.Attribute) and func.attr == "import_module"
    is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
    if not (is_import_module or is_dunder_import):
        return None
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value:
        return _PyImportDemand(
            module=first.value,
            level=0,
            symbol=None,
            lineno=getattr(call, "lineno", 0),
            guarded=guarded,
        )
    return None


def _resolve_python_import_demand(
    demand: _PyImportDemand,
    *,
    importer_rel: str,
    importer_is_package: bool,
    index: _PyFirstPartyIndex,
) -> tuple[str, str, str]:
    """Resolve one demand → ``(kind, module_key, message)``.

    ``kind`` ∈ {``"ok"``, ``"ignore"``, ``"module_missing"``, ``"symbol_missing"``}.
    Only FIRST-PARTY demands are hard-checked; a third-party / unknown target is
    ``ignore`` (anti-false-RED). A guarded import is never ``*_missing``.
    """
    importer_key = index.dotted_key_for_rel(importer_rel)
    module_key = index.resolve_import_target_key(
        importer_key=importer_key,
        importer_is_package=importer_is_package,
        module=demand.module,
        level=demand.level,
    )
    if module_key is None:
        return "ignore", "", ""
    # Is this a first-party target at all? Relative imports are first-party by
    # construction (they resolve within the importer's own package tree); an
    # absolute import is first-party only when its head is an indexed root.
    first_party = demand.level > 0 or index.is_first_party_prefix(module_key)
    if not first_party:
        return "ignore", module_key, ""  # third-party → not our concern
    if not index.resolves(module_key):
        if demand.guarded:
            return "ignore", module_key, ""  # optional first-party plugin — warn-not-fail
        return (
            "module_missing",
            module_key,
            f"first-party module {module_key!r} does not resolve to any generated "
            f"source/test module",
        )
    # Module resolves. Symbol check (only for ``from X import sym`` — module-only
    # demands have symbol=None). The symbol may live in the module OR, if the key
    # is a PACKAGE, name a SUBMODULE (``from pkg import sub`` where sub is sub.py).
    if demand.symbol is None:
        return "ok", module_key, ""
    submodule_key = f"{module_key}.{demand.symbol}"
    if index.resolves(submodule_key):
        return "ok", module_key, ""  # ``from pkg import submodule``
    if index.has_module_getattr(module_key):
        # PEP 562: the target module declares a module-level ``__getattr__`` that
        # provides attributes dynamically, so this symbol's presence is statically
        # UNDECIDABLE — do not flag it missing (false-RED avoidance). Module
        # resolution above is still required (a missing module stays module_missing),
        # and re-exports do NOT inherit this (provided_names stays precise): only a
        # DIRECT named import from the ``__getattr__`` bearer is excused.
        return "ok", module_key, ""
    provided = index.provided_names(module_key)
    if provided is _PY_PROVIDES_UNKNOWN:
        return "ok", module_key, ""  # provider undecidable → never flag (anti-false-RED)
    if demand.symbol in provided:
        return "ok", module_key, ""
    if demand.guarded:
        return "ignore", module_key, ""
    return (
        "symbol_missing",
        module_key,
        f"first-party module {module_key!r} does not define or re-export symbol "
        f"{demand.symbol!r}",
    )


def _run_python_first_party_import_layer(
    project_root: Path,
    profile: LayoutProfile,
    scope: PythonOracleScope,
    files: tuple[str, ...],
) -> PythonToolRun:
    """Resolve every first-party import + imported symbol over ALL source+test .py.

    THE keystone layer: a ``src/app/hidden.py: from .missing import X`` that no
    test imports is invisible to py_compile (no resolution) and to collect-only
    (never imported) — only this static resolver proves the module/symbol absent.
    """
    index = _build_python_module_index(project_root, profile, scope)
    findings: list[ImplementOracleFinding] = []
    observed: list[str] = []
    pkg_rels = {m.rel for m in index.modules.values() if m.is_package}
    for rel in files:
        observed.append(rel)
        tree = _py_parse_file(project_root / rel)
        if tree is None:
            continue  # the compile layer owns unparseable files
        importer_is_package = rel in pkg_rels or rel.endswith("__init__.py")
        for demand in _iter_runtime_import_demands(tree):
            kind, _module_key, message = _resolve_python_import_demand(
                demand,
                importer_rel=rel,
                importer_is_package=importer_is_package,
                index=index,
            )
            if kind == "module_missing":
                findings.append(
                    ImplementOracleFinding(
                        category=EVIDENCE_MODULE_RESOLUTION,
                        code="PY_MODULE_NOT_FOUND",
                        message=message,
                        path=rel,
                    )
                )
            elif kind == "symbol_missing":
                findings.append(
                    ImplementOracleFinding(
                        category=EVIDENCE_MISSING_SYMBOL,
                        code="PY_IMPORT_NAME_NOT_FOUND",
                        message=message,
                        path=rel,
                    )
                )
    return PythonToolRun(
        name="python_first_party_imports",
        executed=True,
        observed_files=tuple(observed),
        findings=tuple(findings),
    )


# ── layer 3: pytest --collect-only (test-surface importability) ──────────────

#: pytest collection-error patterns (multiple errors per run are allowed).
_PYTEST_ERROR_COLLECTING = re.compile(
    r"^_+\s+ERROR collecting (?P<path>.+?)\s+_+$",
    re.MULTILINE,
)
_PYTEST_IMPORT_WHILE = re.compile(
    r"(?:Error|error) while importing test module ['\"](?P<path>.+?)['\"]",
)
_PYTEST_MOD_NOT_FOUND = re.compile(
    r"(?:E\s+)?ModuleNotFoundError:\s+No module named ['\"](?P<module>[^'\"]+)['\"]",
)
_PYTEST_CANNOT_IMPORT_NAME = re.compile(
    r"(?:E\s+)?ImportError:\s+cannot import name ['\"](?P<symbol>[^'\"]+)['\"] from ['\"](?P<module>[^'\"]+)['\"]",
)
_PYTEST_SYNTAX_ERROR = re.compile(
    r"(?:E\s+)?(?P<kind>SyntaxError|IndentationError|TabError):\s+(?P<msg>.+)",
)
#: pytest itself missing / un-spawnable.
_PYTEST_NO_MODULE = re.compile(r"No module named pytest", re.IGNORECASE)


def normalize_python_tool_output(
    output: str,
    *,
    command: str,
    project_root: Path,
    profile: LayoutProfile,
    is_first_party: Callable[[str], bool] | None = None,
) -> tuple[list[ImplementOracleFinding], list[str]]:
    """Normalize ``pytest --collect-only`` output → (findings, failed paths).

    Parses every collection error (ModuleNotFoundError / cannot-import-name /
    SyntaxError / generic ERROR-collecting) into a language-neutral finding. Used
    by the collect layer; also reachable via :func:`normalize_oracle_output`.

    ``is_first_party`` (when supplied) gates module-resolution findings to
    FIRST-PARTY targets only: a collection ``ModuleNotFoundError`` / cannot-import
    -name whose target module is third-party / stdlib is an IMPLEMENT-TIME
    ENVIRONMENT concern (the dependency is simply not installed yet), NOT a
    coherence failure — first-party importability is already proven by the static
    first-party import resolver layer. Emitting it would be a false-RED. A
    first-party target (the SUT genuinely missing a module/symbol) is always
    reported. When ``is_first_party`` is None the legacy behaviour (emit every
    parsed error) is preserved for callers without a module index.
    """
    text = output or ""
    findings: list[ImplementOracleFinding] = []
    failed_paths: list[str] = []

    def _add_path(raw: str | None) -> str | None:
        if not raw:
            return None
        rel = _py_rel_project(project_root / raw.strip(), project_root)
        if rel not in failed_paths:
            failed_paths.append(rel)
        return rel

    for m in _PYTEST_ERROR_COLLECTING.finditer(text):
        _add_path(m.group("path"))
    for m in _PYTEST_IMPORT_WHILE.finditer(text):
        _add_path(m.group("path"))

    primary_path = failed_paths[0] if failed_paths else None
    for m in _PYTEST_MOD_NOT_FOUND.finditer(text):
        module = m.group("module")
        if is_first_party is not None and not is_first_party(module):
            continue  # third-party / stdlib not installed at implement time — env, not coherence
        findings.append(
            ImplementOracleFinding(
                category=EVIDENCE_MODULE_RESOLUTION,
                code="PY_MODULE_NOT_FOUND",
                message=f"No module named {module!r} (pytest collection)",
                path=primary_path,
            )
        )
    for m in _PYTEST_CANNOT_IMPORT_NAME.finditer(text):
        source_module = m.group("module")
        if is_first_party is not None and not is_first_party(source_module):
            continue  # symbol missing from a third-party module — env/version, not SUT coherence
        findings.append(
            ImplementOracleFinding(
                category=EVIDENCE_MISSING_SYMBOL,
                code="PY_IMPORT_NAME_NOT_FOUND",
                message=f"cannot import name {m.group('symbol')!r} from {source_module!r}",
                path=primary_path,
            )
        )
    for m in _PYTEST_SYNTAX_ERROR.finditer(text):
        findings.append(
            ImplementOracleFinding(
                category=EVIDENCE_OTHER,
                code=m.group("kind"),
                message=m.group("msg").strip(),
                path=primary_path,
            )
        )
    return findings, failed_paths


def _python_collect_timeout_seconds(config: Mapping[str, Any] | None) -> float:
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        raw = section.get("python_collect_timeout_seconds")
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return DEFAULT_PYTHON_COLLECT_TIMEOUT_SECONDS


def _collection_failure_is_third_party_only(
    output: str, is_first_party: Callable[[str], bool], errored_file_count: int
) -> bool:
    """True iff EVERY pytest collection error is an uninstalled third-party / stdlib
    import — so the non-zero collect exit is an implement-time ENV concern, not a
    coherence failure.

    ``errored_file_count`` is the number of DISTINCT errored test files (the
    deduped, project-relative ``failed_paths`` the normalizer already computed —
    parsing it here would double-count, since one file surfaces both an
    ``ERROR collecting <rel>`` header and an absolute ``importing test module`` line).

    Anti-false-green (conservative by construction): returns False — i.e. let the
    honest ``pytest_collect_exit_N`` failure stand — UNLESS every errored file is
    accounted for by a non-first-party ``ModuleNotFoundError``, AND there is NO
    first-party module-not-found, NO cannot-import-name from a first-party module,
    and NO SyntaxError. First-party importability/coherence is independently proven
    by the static first-party import-resolver layer (the keystone layer), so a real
    SUT defect cannot hide behind a benign verdict here.
    """
    text = output or ""
    if errored_file_count <= 0:
        return False  # non-zero exit with no identifiable errored file — stay honest
    if _PYTEST_SYNTAX_ERROR.search(text):
        return False
    for m in _PYTEST_CANNOT_IMPORT_NAME.finditer(text):
        if is_first_party(m.group("module")):
            return False
    third_party_module_errors = 0
    for m in _PYTEST_MOD_NOT_FOUND.finditer(text):
        if is_first_party(m.group("module")):
            return False
        third_party_module_errors += 1
    # every errored file must be accounted for by a third-party module-not-found
    return third_party_module_errors >= errored_file_count


def _run_python_pytest_collect_layer(
    project_root: Path,
    profile: LayoutProfile,
    scope: PythonOracleScope,
    config: Mapping[str, Any] | None,
) -> PythonToolRun:
    """``python -m pytest <test_root> --collect-only -q`` — test importability.

    REQUIRED (not optional): the Python profile's runner IS pytest, so pytest
    absent / un-spawnable is an ``environment_build_error`` (NEVER a silent skip —
    a generated system whose tests cannot even be collected is not verified).
    Exit 0 ⇒ the test surface imports cleanly. Non-zero ⇒ parse the collection
    errors; a collection failure caused ONLY by uninstalled third-party imports is
    an env concern (benign — first-party coherence is proven by the resolver
    layer); a non-zero exit with no parseable / unattributable error is still an
    honest failure.
    """
    timeout = _python_collect_timeout_seconds(config)
    test_root = _py_norm(profile.test_root) or "."
    command = (
        f"{shlex.quote(sys.executable)} -m pytest {shlex.quote(test_root)} "
        f"--collect-only -q -p no:cacheprovider"
    )
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return PythonToolRun(
            name="pytest_collect",
            executed=True,
            findings=(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="pytest_collect_timeout",
                    message=f"pytest --collect-only exceeded {timeout:g}s",
                ),
            ),
        )
    except (OSError, ValueError) as exc:
        return PythonToolRun(
            name="pytest_collect",
            executed=False,
            findings=(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="pytest_spawn_error",
                    message=f"could not spawn pytest: {exc}",
                ),
            ),
        )
    output = "\n".join(p for p in (completed.stdout, completed.stderr) if p)
    if _PYTEST_NO_MODULE.search(output):
        return PythonToolRun(
            name="pytest_collect",
            executed=False,
            output=output,
            findings=(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="pytest_not_installed",
                    message=(
                        "pytest is not installed, so the generated test surface cannot "
                        "be collected (the Python profile's runner IS pytest); this is "
                        "an environment failure, not a pass."
                    ),
                ),
            ),
        )
    if completed.returncode == 0:
        return PythonToolRun(name="pytest_collect", executed=True, output=output)
    index = _build_python_module_index(project_root, profile, scope)
    findings, failed_paths = normalize_python_tool_output(
        output,
        command=command,
        project_root=project_root,
        profile=profile,
        is_first_party=index.is_first_party_prefix,
    )
    if not findings:
        if _collection_failure_is_third_party_only(
            output, index.is_first_party_prefix, len(failed_paths)
        ):
            # Collection failed ONLY because a third-party dependency is not
            # installed at implement time. First-party importability is proven by
            # the static resolver layer; this is an environment concern, not a
            # coherence failure → benign (never a false-RED on uninstalled deps).
            return PythonToolRun(name="pytest_collect", executed=True, output=output)
        findings = [
            ImplementOracleFinding(
                category=EVIDENCE_TEST_NOT_COLLECTED,
                code=f"pytest_collect_exit_{completed.returncode}",
                message=(
                    _output_tail(completed.stdout, completed.stderr)
                    or f"pytest --collect-only exited {completed.returncode} with no parseable diagnostic"
                ),
            )
        ]
    return PythonToolRun(
        name="pytest_collect",
        executed=True,
        observed_files=tuple(failed_paths),
        findings=tuple(findings),
        output=output,
    )


# ── optional layer: ruff/pyflakes name lint (SEPARATE registry contract) ─────


def _python_lint_mode(config: Mapping[str, Any] | None) -> str:
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "python_name_lint" in section:
        value = str(section["python_name_lint"]).strip().lower()
        if value in _PY_LINT_MODES:
            return value
    return DEFAULT_PYTHON_NAME_LINT


def _run_python_lint_layer(
    project_root: Path,
    profile: LayoutProfile,
    files: tuple[str, ...],
    *,
    required: bool,
) -> PythonToolRun:
    """Optional ruff/pyflakes undefined-name lint (F821 family).

    OPTIONAL by default: if neither ruff nor pyflakes is importable/runnable, this
    SKIPS (``optional`` mode) — and the composite oracle then does NOT claim
    undefined-local-name coverage (that stays the separate, UNCOVERED
    ``python.undefined_name_lint.v1`` contract). ``required`` mode turns absence
    into an ``environment_build_error``.
    """
    import importlib.util

    have_ruff = _which("ruff") is not None
    have_pyflakes = importlib.util.find_spec("pyflakes") is not None
    if not have_ruff and not have_pyflakes:
        if required:
            return PythonToolRun(
                name="python_name_lint",
                executed=False,
                optional=True,
                findings=(
                    ImplementOracleFinding(
                        category=EVIDENCE_ENVIRONMENT_BUILD,
                        code="name_lint_unavailable",
                        message=(
                            "implement.python_name_lint=required but neither ruff nor "
                            "pyflakes is available to check undefined names"
                        ),
                    ),
                ),
            )
        return PythonToolRun(
            name="python_name_lint",
            executed=False,
            optional=True,
            skipped_reason="ruff/pyflakes not available (optional lint skipped)",
        )
    target_root = _py_norm(profile.source_root) or "."
    if have_ruff:
        command = f"{shlex.quote(_which('ruff'))} check --select F821,F822 --output-format concise {shlex.quote(target_root)}"
    else:
        command = f"{shlex.quote(sys.executable)} -m pyflakes {shlex.quote(target_root)}"
    try:
        completed = subprocess.run(
            command, shell=True, cwd=project_root, capture_output=True, text=True, timeout=300
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        if required:
            return PythonToolRun(
                name="python_name_lint",
                executed=False,
                optional=True,
                findings=(
                    ImplementOracleFinding(
                        category=EVIDENCE_ENVIRONMENT_BUILD,
                        code="name_lint_spawn_error",
                        message=f"could not run name lint: {exc}",
                    ),
                ),
            )
        return PythonToolRun(
            name="python_name_lint",
            executed=False,
            optional=True,
            skipped_reason=f"name lint did not run ({exc}); optional, skipped",
        )
    output = "\n".join(p for p in (completed.stdout, completed.stderr) if p)
    findings: list[ImplementOracleFinding] = []
    if completed.returncode != 0:
        for line in output.splitlines():
            if "F821" in line or "undefined name" in line:
                findings.append(
                    ImplementOracleFinding(
                        category=EVIDENCE_OTHER,
                        code="PY_UNDEFINED_NAME",
                        message=line.strip(),
                    )
                )
    return PythonToolRun(
        name="python_name_lint",
        executed=True,
        observed_files=files,
        findings=tuple(findings),
        output=output,
        optional=True,
    )


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


# ── observability gate (anti-false-green: each required tool MUST observe all) ─


def _certify_python_tool_observability(
    scope: PythonOracleScope, tools: list[PythonToolRun]
) -> list[ImplementOracleFinding]:
    """Honest-fail if a REQUIRED tool did not observe every expected file / run.

    compile + first-party-imports MUST have OBSERVED all expected .py; pytest
    collect MUST have EXECUTED. A gap is an ``environment_build_error`` finding —
    NEVER a silent green (the executor adds these to the result's findings).
    """
    expected = set(scope.expected_files)
    findings: list[ImplementOracleFinding] = []
    by_name = {t.name: t for t in tools}
    for required_name in ("python_compile", "python_first_party_imports"):
        tool = by_name.get(required_name)
        if tool is None or not tool.executed:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="python_oracle_tool_not_executed",
                    message=f"required Python oracle tool {required_name!r} did not execute",
                )
            )
            continue
        missing = sorted(expected - set(tool.observed_files))
        if missing:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="python_oracle_scope_gap",
                    message=(
                        f"{required_name} did not observe {len(missing)} expected .py "
                        f"file(s): " + ", ".join(missing[:12])
                    ),
                )
            )
    collect = by_name.get("pytest_collect")
    if collect is None or not collect.executed:
        # A non-executed collect already carries its own environment finding from the
        # collect layer; add the observability finding only when the layer is wholly
        # absent (defensive — the executor always appends a collect tool).
        if collect is None:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="pytest_collect_not_executed",
                    message="pytest --collect-only did not execute; test importability is unobserved",
                )
            )
    return findings


# ── the composite executor ───────────────────────────────────────────────────


def _run_python_composite_oracle(
    project_root: Path,
    profile: LayoutProfile,
    spec: ImplementOracleSpec,
    config: Mapping[str, Any] | None,
) -> ImplementOracleResult:
    """Run compile + first-party imports + pytest collect (+ optional lint).

    The union of findings gates green: ANY finding ⇒ failed. The observability
    gate is folded in (a tool that didn't observe all files / didn't run is an
    honest environment failure, never a silent pass). ``diagnostics=[]`` (MVP: no
    scoped Python rerun — the bounded loop falls to broad rerun, which is safe).
    """
    scope = _python_oracle_scope(project_root, profile, spec)
    all_files = scope.expected_files
    tools: list[PythonToolRun] = [
        _run_python_compile_layer(project_root, all_files),
        _run_python_first_party_import_layer(project_root, profile, scope, all_files),
        _run_python_pytest_collect_layer(project_root, profile, scope, config),
    ]
    lint_mode = _python_lint_mode(config)
    if lint_mode != "off":
        tools.append(
            _run_python_lint_layer(
                project_root, profile, all_files, required=(lint_mode == "required")
            )
        )

    findings: list[ImplementOracleFinding] = []
    failed_paths: list[str] = []
    raw_parts: list[str] = []
    for tool in tools:
        findings.extend(tool.findings)
        body = tool.output or tool.skipped_reason or ("(no findings)" if tool.executed else "(not executed)")
        raw_parts.append(f"## {tool.name} (executed={tool.executed})\n{body}")
        for f in tool.findings:
            if f.path and f.path not in failed_paths:
                failed_paths.append(f.path)
    findings.extend(_certify_python_tool_observability(scope, tools))

    passed = not findings
    return ImplementOracleResult(
        passed=passed,
        executed=True,
        command="python-composite: compile + first-party-imports + pytest --collect-only",
        findings=findings,
        failed_paths=failed_paths,
        detail=(
            f"python composite oracle {'passed' if passed else 'failed'}; "
            f"{len(scope.source_files)} source file(s), {len(scope.test_files)} test "
            f"file(s), {len(findings)} finding(s)"
        ),
        raw_output="\n\n".join(raw_parts),
        diagnostics=[],
    )


# ═════════════════════════════════════════════════════════════════════════════
# Go COMPOSITE implement-oracle (the compiler-class equivalent of tsc --noEmit).
# Design: dogfood/gpt_language_generality_design.md §1.4–1.5 (go.yaml declares the
# commands; the oracle EXECUTION + tool-output parsing is the unavoidable adapter).
#
# WHY GO NEEDS THIS (the false-green it closes): before this, the implement gate
# NO-OPed for Go (``resolve_layout_profile('go')`` returns None — Go has no single
# ``source_root``, so the legacy LayoutProfile/compat shim deliberately refuses to
# build one). A Go package that does NOT compile (undefined symbol, missing
# first-party import) therefore sailed through implement UNCHECKED. This composite
# makes Go reachable WITHOUT a legacy LayoutProfile: it is selected by the same
# ``language``/``kind`` dispatch the Python composite uses (one entry in
# :func:`_run_oracle_command`), and its (profile, spec) are synthesized in
# :func:`resolve_implement_oracle` from the declarative ``go`` profile in
# ``codd.languages.registry`` (the language→oracle map the design calls for).
#
# TWO COMMANDS (both from go.yaml, run from the module root):
#   1. ``go build ./...``  — compile + import resolution across the whole module.
#   2. ``go vet ./...``    — the typechecker (catches ``undefined: X`` etc.) PLUS
#                            suspicious-construct analysis. ``go vet`` runs the full
#                            type-check first, so it is the primary coherence proof;
#                            ``go build`` adds binary-linking errors vet may miss.
#
# ANTI-FALSE-GREEN + THIRD-PARTY TOLERANCE (mirrors the Python oracle):
#   * ``undefined: X`` (a missing symbol the SUT must define)            → RED.
#   * ``cannot find module providing package P`` / ``package P is not in
#     std`` where P is FIRST-PARTY (P == module_path or starts with
#     module_path + "/")                                                 → RED.
#   * the SAME "cannot find module providing package" for a THIRD-PARTY P
#     (not module-path-prefixed) is an implement-time ENVIRONMENT concern
#     (the dep is simply not downloaded under ``-mod=readonly``), NOT a
#     coherence failure                                                  → TOLERATED
#     (no false-RED on uninstalled third-party — exactly the Python oracle's
#     "first-party provably absent → fail; third-party/unknown → never fail").
#   * a generic ``path:line:col: message`` compile diagnostic              → other.
#   * Go's VCS-stamping noise (``error obtaining VCS status`` — emitted by
#     ``go build`` for a main package when no usable git repo is present) is an
#     ENVIRONMENT artifact, never a code-coherence signal → it is FILTERED OUT and
#     does not RED a build (``go vet`` does not stamp VCS, so it stays the clean
#     authority).
# ═════════════════════════════════════════════════════════════════════════════

#: A Go tool diagnostic with a file position: ``path:line:col: message`` — the
#: canonical compiler/vet line. An optional leading ``vet: `` prefix (vet emits
#: ``vet: ./file.go:..``) and a leading ``./`` on the path are tolerated. The
#: trailing message is captured so the symbol/module sub-classifier can run.
_GO_DIAG_LINE = re.compile(
    r"^(?:vet:\s*)?(?P<path>\.?/?[^\s:][^:\n]*\.go):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+?)\s*$"
)

#: ``go build``/``go vet`` group package diagnostics under a ``# <import-path>``
#: header (and sometimes a ``# [<import-path>]`` variant). These are context
#: markers, not diagnostics — skipped.
_GO_PKG_HEADER = re.compile(r"^#\s")

#: ``undefined: <name>`` — a symbol the package demands that nothing defines. The
#: Go analogue of TS2304/Python PY_IMPORT_NAME_NOT_FOUND → missing_symbol (RED).
_GO_UNDEFINED_RE = re.compile(r"\bundefined:\s*(?P<symbol>[\w.]+)")

#: ``cannot find module providing package <P>[: ...]`` and ``package <P> is not in
#: std (...)`` — the import-not-resolvable family. ``<P>`` is the IMPORT PATH; it
#: is classified first-party-vs-third-party by the module path (see
#: ``_go_is_first_party_import``) so an uninstalled THIRD-PARTY dep is tolerated.
_GO_CANNOT_FIND_PKG_RE = re.compile(
    r"cannot find (?:module providing )?package\s+(?P<pkg>[^\s:]+)"
)
_GO_NOT_IN_STD_RE = re.compile(r"package\s+(?P<pkg>\S+)\s+is not in std\b")

#: ``go build``'s VCS-stamping failure (a main package built outside a usable git
#: repo). An ENVIRONMENT artifact — filtered out, never a code-coherence RED.
_GO_VCS_STAMP_RE = re.compile(
    r"error obtaining VCS status|\buse -buildvcs=false\b", re.IGNORECASE
)


def _go_is_first_party_import(import_path: str, module_path: str) -> bool:
    """True when ``import_path`` is the module itself or a package under it.

    Go's first-party rule (design §1.5): ``import == module_path`` OR
    ``import.startswith(module_path + "/")``. Everything else (stdlib, an external
    ``github.com/...`` dep, a relative ``./x``) is NOT first-party — so a
    "cannot find module providing package" for it is an uninstalled-dependency
    ENVIRONMENT concern, not a coherence failure (anti-false-RED). With no known
    module path (undecidable), NOTHING is first-party — the conservative side that
    never invents a false-RED.
    """
    mod = (module_path or "").strip().strip("/")
    pkg = (import_path or "").strip()
    if not mod or not pkg:
        return False
    return pkg == mod or pkg.startswith(mod + "/")


def _classify_go_diagnostic(
    message: str, *, module_path: str
) -> tuple[str, str] | None:
    """Classify one Go diagnostic message → ``(category, code)`` or ``None``.

    ``None`` ⇒ TOLERATE (an uninstalled third-party import — an env concern, not a
    coherence failure). Otherwise the language-neutral category + a Go-specific
    code. Mirrors the Python resolver's "first-party provably absent → fail;
    third-party/unknown → never fail" policy.
    """
    undef = _GO_UNDEFINED_RE.search(message)
    if undef is not None:
        return EVIDENCE_MISSING_SYMBOL, "GO_UNDEFINED"
    cannot_find = _GO_CANNOT_FIND_PKG_RE.search(message)
    not_in_std = _GO_NOT_IN_STD_RE.search(message)
    pkg_match = cannot_find or not_in_std
    if pkg_match is not None:
        pkg = pkg_match.group("pkg")
        if _go_is_first_party_import(pkg, module_path):
            return EVIDENCE_MODULE_RESOLUTION, "GO_PACKAGE_NOT_FOUND"
        # Third-party / stdlib-shaped import that does not resolve at implement
        # time → uninstalled dependency (env), not SUT incoherence → tolerate.
        return None
    # A real positioned compile diagnostic that is neither of the above (a type
    # error, redeclaration, …) is still a coherence failure → other.
    return EVIDENCE_OTHER, "GO_COMPILE_ERROR"


def _parse_go_tool_output(
    output: str, *, tool: str, module_path: str, project_root: Path
) -> tuple[list[ImplementOracleFinding], list[str]]:
    """Parse ``go build``/``go vet`` output → (findings, editable failed paths).

    Walks positioned ``path:line:col: message`` lines, skips ``# pkg`` headers and
    VCS-stamping noise, and classifies each via :func:`_classify_go_diagnostic`
    (third-party-tolerant). Lines WITHOUT a file position that still name a
    not-found package are also classified (Go sometimes emits the import error on
    a bare ``path:col`` form already covered, or — for ``go list`` style — without
    a column; the positioned form covers the build/vet cases observed).
    """
    findings: list[ImplementOracleFinding] = []
    failed_paths: list[str] = []
    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _GO_PKG_HEADER.match(line):
            continue
        if _GO_VCS_STAMP_RE.search(line):
            continue  # environment noise, never a code-coherence finding
        m = _GO_DIAG_LINE.match(line)
        if m is None:
            continue
        message = m.group("message").strip()
        classified = _classify_go_diagnostic(message, module_path=module_path)
        if classified is None:
            continue  # tolerated (uninstalled third-party)
        category, code = classified
        rel = _go_rel_path(m.group("path"), project_root)
        findings.append(
            ImplementOracleFinding(
                category=category,
                code=code,
                message=f"[{tool}] {message}",
                path=rel,
            )
        )
        if rel and rel not in failed_paths:
            failed_paths.append(rel)
    return findings, failed_paths


def _go_rel_path(raw: str, project_root: Path) -> str | None:
    """Normalize a Go diagnostic path (possibly ``./x.go``) → project-relative."""
    cleaned = (raw or "").strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned:
        return None
    try:
        resolved = (project_root / cleaned).resolve()
        return resolved.relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(cleaned.replace("\\", "/")).as_posix()


def _go_module_path(project_root: Path) -> str:
    """Read the ``module`` directive from ``<root>/go.mod`` (the first-party prefix).

    Empty string when go.mod is missing/unreadable or has no module line — the
    classifier then treats EVERY import as not-first-party (the conservative,
    never-a-false-RED side; the certifier already hard-fails a missing go.mod).
    """
    gomod = project_root / "go.mod"
    try:
        text = gomod.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("module "):
            return stripped[len("module ") :].strip().strip('"')
    return ""


def _go_command_argv(command_id: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve a go.yaml command's ``argv`` from the declarative profile.

    Reads ``commands.<command_id>.argv`` from the registry's ``go`` profile so the
    oracle stays PROFILE-DRIVEN (the design's "command argv lives in data"). Falls
    back to ``default`` if the profile/registry is unavailable for any reason —
    the oracle must run even if profile discovery hiccups (the defaults are the
    exact go.yaml values).
    """
    try:
        from codd.languages import default_registry

        profile = default_registry.resolve("go")
        spec = profile.commands.get(command_id)
        if spec is not None and spec.argv:
            return tuple(spec.argv)
    except Exception:  # noqa: BLE001 — profile discovery is best-effort; use defaults.
        pass
    return default


def _go_command_env(command_id: str) -> dict[str, str]:
    """Resolve a go.yaml command's ``env`` (e.g. ``GOFLAGS: -mod=readonly``)."""
    try:
        from codd.languages import default_registry

        profile = default_registry.resolve("go")
        spec = profile.commands.get(command_id)
        if spec is not None and spec.env:
            return {str(k): str(v) for k, v in spec.env.items()}
    except Exception:  # noqa: BLE001 — best-effort; the go.yaml default is -mod=readonly.
        pass
    return {"GOFLAGS": "-mod=readonly"}


def _run_one_go_command(
    *,
    command_id: str,
    default_argv: tuple[str, ...],
    module_root: Path,
    module_path: str,
    config: Mapping[str, Any] | None,
) -> tuple[bool, list[ImplementOracleFinding], list[str], str]:
    """Run one go command → ``(spawned, findings, failed_paths, raw_output)``.

    ``spawned`` is False only when the tool could not be executed at all (go
    missing / spawn error) — surfaced as an ``environment_build_error`` finding,
    never a silent skip. A non-zero exit whose output yields NO code findings (e.g.
    ONLY VCS-stamping noise) is benign here: the other command (vet) stays the
    coherence authority.
    """
    argv = _go_command_argv(command_id, default_argv)
    env_overrides = _go_command_env(command_id)
    command_str = " ".join(argv)
    timeout = _go_oracle_timeout_seconds(config)
    import os

    run_env = dict(os.environ)
    run_env.update(env_overrides)
    try:
        completed = subprocess.run(
            list(argv),
            cwd=module_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
        )
    except subprocess.TimeoutExpired:
        return (
            True,
            [
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="go_oracle_timeout",
                    message=f"`{command_str}` exceeded {timeout:g}s",
                )
            ],
            [],
            "",
        )
    except (OSError, ValueError) as exc:
        return (
            False,
            [
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="go_spawn_error",
                    message=f"could not run `{command_str}` (is the Go toolchain on PATH?): {exc}",
                )
            ],
            [],
            "",
        )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    findings, failed_paths = _parse_go_tool_output(
        output, tool=command_id, module_path=module_path, project_root=module_root
    )
    if completed.returncode != 0 and not findings:
        # Non-zero exit with no PARSEABLE code finding. A non-zero exit is BENIGN
        # when every output line is either filtered environment noise (VCS
        # stamping / pkg header) OR a DELIBERATELY-TOLERATED diagnostic (an
        # uninstalled third-party import under -mod=readonly — the dep simply is
        # not downloaded at implement time; vet/build over first-party stays the
        # authority). Only an UNEXPLAINED non-zero exit with leftover non-noise,
        # non-tolerated output is an honest opaque environment failure (never a
        # silent pass). This mirrors the Python collect layer's
        # ``_collection_failure_is_third_party_only`` benign-verdict.
        if _go_residual_is_benign(output, module_path=module_path):
            return True, [], [], output
        findings = [
            ImplementOracleFinding(
                category=EVIDENCE_ENVIRONMENT_BUILD,
                code=f"go_{command_id}_exit_{completed.returncode}",
                message=_output_tail(completed.stdout, completed.stderr)
                or f"`{command_str}` exited {completed.returncode} with no parseable diagnostic",
            )
        ]
    return True, findings, failed_paths, output


def _go_residual_is_benign(output: str, *, module_path: str) -> bool:
    """True iff a non-zero go exit's output is ONLY noise / tolerated diagnostics.

    Returns True (benign — let the non-zero exit pass) when EVERY non-blank line is
    a ``# pkg`` header, VCS-stamping noise, or a positioned diagnostic that the
    third-party-tolerant classifier deliberately TOLERATES (an uninstalled
    third-party import). Returns False — i.e. surface an honest opaque failure — the
    moment any line is unaccounted-for (a non-noise, non-diagnostic line), so a
    genuinely opaque toolchain error never hides behind a benign verdict
    (anti-false-green: conservative by construction).
    """
    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _GO_PKG_HEADER.match(line) or _GO_VCS_STAMP_RE.search(line):
            continue
        m = _GO_DIAG_LINE.match(line)
        if m is not None and _classify_go_diagnostic(
            m.group("message").strip(), module_path=module_path
        ) is None:
            continue  # a tolerated (uninstalled third-party) diagnostic
        return False  # an unaccounted-for line → not benign
    return True


def _go_oracle_timeout_seconds(config: Mapping[str, Any] | None) -> float:
    """Bounded wall-clock for ONE go command (``implement.oracle_timeout_seconds``).

    Reuses the shared oracle-timeout knob (the same one the TS ``tsc`` run uses) so
    a cold ``go build`` of a large module has the generous-but-bounded budget.
    """
    return _oracle_timeout_seconds(config)


def _run_go_composite_oracle(
    project_root: Path,
    profile: LayoutProfile,
    spec: ImplementOracleSpec,
    config: Mapping[str, Any] | None,
) -> ImplementOracleResult:
    """Run ``go build ./...`` + ``go vet ./...`` from the module root; union findings.

    ANY real finding ⇒ failed (RED). A clean module (both commands clean, modulo
    tolerated third-party/VCS noise) ⇒ passed. A spawn failure on EITHER command is
    an honest ``environment_build_error`` (the gate's retry loop will not burn
    reruns on it — see :func:`_only_environment`). ``diagnostics=[]`` (no scoped Go
    rerun yet — the bounded loop falls to a safe broad rerun, exactly like Python).
    """
    module_root = project_root / _go_norm_module_root(profile)
    module_path = _go_module_path(module_root)

    findings: list[ImplementOracleFinding] = []
    failed_paths: list[str] = []
    raw_parts: list[str] = []
    any_spawn_failure = False
    for command_id, default_argv in (
        ("build", ("go", "build", "./...")),
        ("vet", ("go", "vet", "./...")),
    ):
        spawned, cmd_findings, cmd_failed, raw = _run_one_go_command(
            command_id=command_id,
            default_argv=default_argv,
            module_root=module_root,
            module_path=module_path,
            config=config,
        )
        if not spawned:
            any_spawn_failure = True
        findings.extend(cmd_findings)
        for p in cmd_failed:
            if p not in failed_paths:
                failed_paths.append(p)
        raw_parts.append(f"## go {command_id} (spawned={spawned})\n{raw or '(no output)'}")

    passed = not findings and not any_spawn_failure
    return ImplementOracleResult(
        passed=passed,
        executed=True,
        command="go-composite: go build ./... + go vet ./...",
        findings=findings,
        failed_paths=failed_paths,
        detail=(
            f"go composite oracle {'passed' if passed else 'failed'}; "
            f"module_path={module_path or '(unknown)'}, {len(findings)} finding(s)"
        ),
        raw_output="\n\n".join(raw_parts),
        diagnostics=[],
    )


def _go_norm_module_root(profile: LayoutProfile) -> str:
    """The module root the go commands run from (project-relative, ``.`` default).

    Carried on the synthesized Go ``LayoutProfile`` as ``source_root`` (Go's
    ``module_root`` from go.yaml — ``.`` for the repo-root go.mod layout). Empty /
    ``.`` both mean the project root.
    """
    root = _norm(getattr(profile, "source_root", "") or "")
    return root or "."


# ── command execution (REUSES the verify stage's run/attribution shape) ──


def _run_node_install(project_root: Path, config: Mapping[str, Any] | None) -> ImplementOracleResult | None:
    """Blocking dependency install so ``tsc`` + deps are materialized.

    Mirrors the verify stage's install preflight
    (:func:`codd.project_types.node_install_command`): an install FAILURE is an
    honest ``environment_build_error`` — NOT a code-repair target — so it is
    returned as a failed (non-retryable) result the caller turns into a hard
    stage error, never handed to the SUT to "fix" in source.
    """
    command = node_install_command(project_root)
    timeout = _install_timeout(config)
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ImplementOracleResult(
            passed=False,
            executed=True,
            command=command,
            findings=[
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="install_timeout",
                    message=f"dependency install exceeded {timeout:g}s",
                )
            ],
            detail=f"dependency install timed out after {timeout:g}s",
        )
    if completed.returncode != 0:
        tail = _output_tail(completed.stdout, completed.stderr)
        return ImplementOracleResult(
            passed=False,
            executed=True,
            command=command,
            findings=[
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="install_failed",
                    message=f"dependency install failed (exit {completed.returncode})",
                )
            ],
            detail=f"dependency install failed (exit {completed.returncode}): {command}\n{tail}",
            raw_output=tail,
        )
    return None


def _install_timeout(config: Mapping[str, Any] | None) -> float:
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        raw = section.get("oracle_install_timeout_seconds")
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return DEFAULT_ORACLE_INSTALL_TIMEOUT_SECONDS


def _run_oracle_command(
    project_root: Path,
    profile: LayoutProfile,
    spec: ImplementOracleSpec,
    config: Mapping[str, Any] | None,
) -> ImplementOracleResult:
    """Run the native oracle ONCE and normalize the result.

    The command is the profile's (TS: ``npx --no-install tsc --noEmit``), run
    from the project root with a bounded timeout. Exit 0 → passed (the scope was
    already certified by the caller, so this is a TRUE green). Non-zero → parse +
    normalize the diagnostics.

    Python (``spec.kind == "composite"``) does NOT shell out to a single command;
    it dispatches to the in-process multi-tool executor
    (:func:`_run_python_composite_oracle`) — compile + first-party import
    resolver + ``pytest --collect-only`` — each with its own observability gate.
    """
    if profile.language == "python" and spec.kind == "composite":
        return _run_python_composite_oracle(project_root, profile, spec, config)
    if profile.language == "go" and spec.kind == "composite":
        return _run_go_composite_oracle(project_root, profile, spec, config)
    command = spec.command
    timeout = _oracle_timeout_seconds(config)
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ImplementOracleResult(
            passed=False,
            executed=True,
            command=command,
            findings=[
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="oracle_timeout",
                    message=f"native oracle exceeded {timeout:g}s",
                )
            ],
            detail=f"native oracle timed out after {timeout:g}s: {command}",
        )
    except (OSError, ValueError) as exc:
        return ImplementOracleResult(
            passed=False,
            executed=False,
            command=command,
            findings=[
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="oracle_spawn_error",
                    message=f"could not run native oracle: {exc}",
                )
            ],
            detail=f"could not run native oracle ({exc}): {command}",
        )

    full_output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode == 0 and not _TS_NO_INPUTS_RE.search(full_output):
        return ImplementOracleResult(
            passed=True,
            executed=True,
            command=command,
            detail="native oracle passed (typecheck clean)",
            raw_output=full_output,
        )

    findings, failed_paths = normalize_oracle_output(
        full_output, command=command, project_root=project_root, profile=profile
    )
    if not findings:
        # Non-zero exit but no parseable diagnostic — treat as an honest opaque
        # failure (environment/toolchain), never a silent pass.
        findings = [
            ImplementOracleFinding(
                category=EVIDENCE_ENVIRONMENT_BUILD,
                code=f"exit_{completed.returncode}",
                message=(_output_tail(completed.stdout, completed.stderr) or "non-zero exit, no diagnostics"),
            )
        ]
    # Structured diagnostics for the scoped rerun + the loop-breaking signature.
    # Best-effort: a parser failure must never abort the gate (the scope layer
    # degrades to broad on empty diagnostics).
    diagnostics: list[Any] = []
    try:
        from codd.implement_oracle_scope import _parse_ts_diagnostics

        diagnostics = _parse_ts_diagnostics(full_output, project_root)
    except Exception:  # noqa: BLE001 — structured-diag parsing is enrichment only.
        diagnostics = []
    return ImplementOracleResult(
        passed=False,
        executed=True,
        command=command,
        findings=findings,
        failed_paths=failed_paths,
        detail=f"native oracle failed (exit {completed.returncode}); {len(findings)} diagnostic(s)",
        raw_output=full_output,
        diagnostics=diagnostics,
    )


def _output_tail(stdout: str | None, stderr: str | None, limit: int = 4000) -> str:
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
    if len(combined) <= limit:
        return combined
    return f"... (truncated) ...\n{combined[-limit:]}"


# ── public entry: the stage-level gate ──


def resolve_implement_oracle(
    project_root: Path,
    *,
    language: str | None,
    project_name: str | None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Mapping[str, Any] | None = None,
    profile: LayoutProfile | None = None,
) -> tuple[LayoutProfile, ImplementOracleSpec] | None:
    """Resolve the (profile, oracle spec) for a stack, or ``None`` if no oracle.

    ``None`` (the strict NO-OP signal) when: the stack has no layout profile, the
    profile declares no ``implement_oracle`` (Python today), or the gate is
    opted out. The caller treats ``None`` as "this stack has no implement-time
    oracle — skip silently".

    GO (the language→oracle map entry): ``resolve_layout_profile('go')`` returns
    None — Go has no single ``source_root`` so the legacy LayoutProfile/compat
    shim deliberately refuses to build one. Rather than NO-OP (a false-green: a
    non-compiling Go module would pass the implement gate UNCHECKED), we SYNTHESIZE
    a minimal Go ``(LayoutProfile, ImplementOracleSpec)`` from the declarative
    ``go`` profile in ``codd.languages.registry``: a ``kind="composite"`` spec the
    SAME dispatch in :func:`_run_oracle_command` routes to
    :func:`_run_go_composite_oracle` (``go build ./...`` + ``go vet ./...``). This
    is the one registration point for Go's oracle — no ``if language=='go'``
    scattered in the gate.
    """
    if _oracle_opt_out(config):
        return None
    if profile is None:
        profile = resolve_layout_profile(
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
            project_root=project_root,
        )
    if profile is None:
        # No legacy LayoutProfile — try the declarative registry (Go: a composite
        # oracle synthesized from go.yaml, reachable WITHOUT a legacy profile).
        return _resolve_registry_composite_oracle(language)
    if profile.implement_oracle is None:
        return None
    return profile, profile.implement_oracle


#: Languages whose implement oracle is a COMPOSITE synthesized from the declarative
#: ``codd.languages.registry`` profile (no legacy ``LayoutProfile`` builder). The
#: language→oracle map the design calls for; today only Go (Rust later: one entry
#: here + one ``_run_oracle_command`` dispatch + a ``cargo check`` runner).
_REGISTRY_COMPOSITE_ORACLE_LANGUAGES = frozenset({"go", "golang"})


def _resolve_registry_composite_oracle(
    language: str | None,
) -> tuple[LayoutProfile, ImplementOracleSpec] | None:
    """Synthesize a composite ``(LayoutProfile, spec)`` from the registry, or None.

    For a language whose declarative profile (``codd/languages/profiles/<x>.yaml``)
    declares a composite ``implement_oracle`` but has NO legacy ``LayoutProfile``
    builder (Go — ``package_root.kind == none``), build a MINIMAL ``LayoutProfile``
    carrying just ``language`` + the module root (as ``source_root``) so the
    existing gate machinery (run/certify/retry) works unchanged, plus a
    ``kind="composite"`` ``ImplementOracleSpec`` the dispatch routes to the
    per-language composite executor. ``None`` for any other language (back-compat:
    a stack with neither a legacy profile nor a registry composite oracle stays a
    strict NO-OP). Best-effort: a registry/profile error degrades to NO-OP (never a
    crash; the existing verify-stage gates stay the backstop).
    """
    lang = (language or "").strip().lower()
    if lang not in _REGISTRY_COMPOSITE_ORACLE_LANGUAGES:
        return None
    try:
        from codd.languages import default_registry

        lang_profile = default_registry.resolve(lang)
    except Exception:  # noqa: BLE001 — no registry profile ⇒ NO-OP, not a crash.
        return None
    # implement_oracle is now a modeled first-class field (Contract Kernel §1),
    # no longer a raw mapping in ``.extra``. Behaviour is preserved: only a Go
    # profile declaring kind="composite" synthesizes the composite spec below.
    oracle_decl = lang_profile.implement_oracle
    if oracle_decl is None or oracle_decl.kind != "composite":
        return None
    module_root = _norm(getattr(lang_profile.layout, "module_root", ".") or ".") or "."
    synthetic = LayoutProfile(
        language=lang_profile.id,  # canonical id ("go"), so the dispatch matches
        package_name=lang_profile.id,
        source_root=module_root,  # carries the module root for the go commands' cwd
        package_root=module_root,
        test_root=module_root,
        implement_oracle=ImplementOracleSpec(
            command=f"{lang_profile.id}-composite",  # sentinel; kind dispatch runs the executor
            kind="composite",
            scope=OracleScopeSpec(require_source_root=True, require_test_root=False),
            requires_node_install=False,
        ),
    )
    return synthetic, synthetic.implement_oracle


#: The rerun callback the gate invokes to re-implement under the oracle feedback.
#: ``scope is None`` ⇒ BROAD rerun (re-implement every task — the escalation
#: fallback, the legacy behaviour). A non-None ``OracleRerunScope`` ⇒ a SCOPED
#: rerun (re-implement only the scope's tasks, under its write-fence). The second
#: positional is kept optional in spirit — a callback that ignores it falls back
#: to broad, preserving back-compat for any plain ``Callable[[str], None]`` only
#: if it accepts the extra arg; the pipeline's callback consumes the scope.
OracleRerunCallback = Callable[[str, "Any"], None]


def run_implement_oracle_gate(
    project_root: Path | str,
    *,
    language: str | None,
    project_name: str | None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Mapping[str, Any] | None = None,
    rerun: OracleRerunCallback | Callable[[str], None] | None = None,
    echo: Callable[[str], None] = print,
    profile: LayoutProfile | None = None,
    scope_index: Any = None,
    structured_source: Any = None,
    manifest_paths: Any = None,
) -> ImplementOracleResult:
    """Run the implement-time native-oracle gate (stage-level, once).

    Sequence:
      1. Resolve the stack's oracle spec. No oracle declared → a passing NO-OP
         (Python, bash, …) — the verify-stage gates stay the backstop.
      2. (node) Run the BLOCKING dependency install so ``tsc`` + deps exist; an
         install failure is an honest ``environment_build_error`` (no retry).
      3. CERTIFY the oracle scope covers source + tests (raises
         :class:`OracleScopeError` on an uncertifiable scope — anti-false-green).
      4. Run the oracle. On failure, derive a SCOPED rerun and re-invoke
         implementation through ``rerun(feedback, scope)`` up to a bounded cap,
         re-running the oracle each time. Returns the FINAL result.

    SCOPED RERUN + ESCALATION LADDER (the localized-rerun design)
    -------------------------------------------------------------
    A broad rerun (regenerate every task) is correct but slow. When a
    ``scope_index`` (a ``codd.implement_oracle_scope.TaskOutputIndex``, the
    path→owning-task map) is supplied, the gate derives the BOTH-ENDS-OF-THE-
    BROKEN-EDGE scope from the diagnostics and re-implements only those tasks.
    Broad is DEMOTED to a fallback rung. The ladder — driven by the diagnostic
    SIGNATURE (the loop-breaker) — is:

        narrow edge scope → expanded one-hop scope → broad → fail honestly

    Escalation triggers: the diagnostics have no determinable owner (→ broad);
    the SAME signature survives a scoped rerun (the scope was too small →
    next rung); or a breadth/fan-out guard fires inside the derivation (→ broad).
    Without a ``scope_index`` the gate behaves exactly as before — a broad rerun
    every attempt — so the change is opt-in via the index.

    The caller (greenfield ``_stage_implement``) turns a non-passing result into
    a :class:`StageError`. ``OracleScopeError`` propagates (it is a hard
    configuration failure, distinct from a curable incoherence).
    """
    root = Path(project_root)
    resolved = resolve_implement_oracle(
        root,
        language=language,
        project_name=project_name,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
        config=config,
        profile=profile,
    )
    if resolved is None:
        return ImplementOracleResult(
            passed=True,
            executed=False,
            command="",
            detail=f"no implement-time oracle for language {language!r} (skipped)",
        )
    profile, spec = resolved

    # 2. Blocking dependency install (node stacks) — must precede certification +
    # run so ``tsc`` is materialized; an install failure is non-retryable.
    if spec.requires_node_install:
        install_failure = _run_node_install(root, config)
        if install_failure is not None:
            echo(f"[greenfield] implement-oracle: {install_failure.detail}")
            return install_failure

    # 3. Certify scope — HARD FAIL on an uncertifiable scope (raises).
    certification = certify_oracle_scope(root, profile, spec)
    echo(f"[greenfield] implement-oracle: {certification}")

    # 4. Run + bounded retry-with-feedback, escalating the rerun scope.
    max_attempts = _oracle_max_attempts(config)
    result = _run_oracle_command(root, profile, spec, config)
    attempt = 1
    rung = _SCOPE_NARROW if scope_index is not None else _SCOPE_BROAD
    last_signature: tuple[Any, ...] | None = None
    #: Bounded history of EARLIER signatures (before ``last_signature``) for cycle
    #: detection. Small window — the cap is small, so a long cycle would exhaust the
    #: budget before recurring; this catches the tight A↔B↔A loop.
    signature_history: list[tuple[Any, ...]] = []
    #: Rungs that have already spent their ONE soft-progress allowance. A rung gets
    #: at most one "fewer-but-some-new" pass before soft progress also escalates —
    #: this is the ``narrow 2nd attempt only while progressing`` budget.
    soft_progress_used: set[str] = set()
    while not result.passed and result.executed and rerun is not None and attempt < max_attempts:
        # Only retry CURABLE incoherence — an environment/toolchain failure is not
        # something the SUT can fix in source, so do not burn retries on it.
        if _only_environment(result):
            break

        # Loop-breaker (progress/oscillation, set-based — NOT exact equality). The
        # previous design escalated only when the signature was IDENTICAL twice,
        # which mis-read oscillation (a SUT inventing different errors each rerun)
        # as progress and stayed pinned at one rung until the budget drained. Now
        # we classify the SET relation to the previous signature and escalate on
        # oscillation/stuck/cycle, keep the rung on a strict shrink, and allow a
        # "fewer-but-some-new" soft step AT MOST ONCE per rung. (See
        # ``classify_signature_progress``.)
        signature = _diagnostic_signature(result)
        if last_signature is not None and scope_index is not None:
            verdict = _classify_progress(signature, last_signature, signature_history)
            should_escalate, escalate_reason = _escalation_decision(
                verdict, rung=rung, soft_progress_used=soft_progress_used
            )
            if should_escalate:
                escalated = _next_rung(rung)
                if escalated is None:
                    echo(
                        f"[greenfield] implement-oracle: {escalate_reason} at broad rerun — "
                        "stopping (honest failure)."
                    )
                    break
                echo(
                    f"[greenfield] implement-oracle: {escalate_reason} after {rung} rerun — "
                    f"escalating scope to {escalated}."
                )
                rung = escalated
            else:
                echo(
                    f"[greenfield] implement-oracle: {verdict} at {rung} rerun — "
                    f"keeping scope (the repair is converging)."
                )
        # Record history AFTER the comparison: ``last_signature`` rolls into the
        # bounded window so a later run can detect a cycle back to it.
        if last_signature is not None:
            signature_history.append(last_signature)
            if len(signature_history) > _SIGNATURE_HISTORY_WINDOW:
                signature_history.pop(0)
        last_signature = signature

        # Derive the scope for this rung (broad when no index, or when the
        # derivation's guards force it). A broad scope is passed as ``None`` so a
        # callback that distinguishes scoped-vs-broad sees the legacy signal.
        scope, forced_broad = _derive_scope_for_rung(
            result=result,
            project_root=root,
            scope_index=scope_index,
            rung=rung,
            structured_source=structured_source,
            manifest_paths=manifest_paths,
            echo=echo,
            legacy_broad=_oracle_legacy_broad_enabled(config),
        )
        if forced_broad and scope_index is not None:
            rung = _SCOPE_BROAD  # a forced-broad derivation pins the rung (monotonic ladder)

        # BROAD-CAMPAIGN branch: a wide-fan-out artifact yields a scope carrying a
        # BroadRepairPlan. Instead of one rerun + one recheck, run the budgeted
        # residual-coherence campaign (supplier-first → residual importers →
        # chunked broad), re-running the WHOLE-PROJECT oracle after every phase.
        # The whole-project oracle is the ONLY green authority; the campaign
        # honest-fails on budget exhaustion / non-convergence (returns the failing
        # result, which the caller turns into a StageError). Counts as ONE outer
        # attempt (the campaign-count limit ``oracle_max_attempts`` still bounds how
        # many campaigns can run).
        if scope is not None and bool(getattr(scope, "is_broad_campaign", lambda: False)()):
            echo(
                f"[greenfield] implement-oracle: {result.detail}; entering broad repair "
                f"campaign (attempt {attempt}/{max_attempts - 1}) — {scope.detail}"
            )
            result = _execute_broad_campaign(
                result=result,
                plan=scope.repair_plan,
                project_root=root,
                profile=profile,
                spec=spec,
                config=config,
                rerun=rerun,
                scope_index=scope_index,
                structured_source=structured_source,
                manifest_paths=manifest_paths,
                echo=echo,
            )
            attempt += 1
            continue

        scope_label = (
            "broad" if scope is None or getattr(scope, "is_broad", lambda: False)() else getattr(scope, "rung", rung)
        )
        echo(
            f"[greenfield] implement-oracle: {result.detail}; "
            f"re-running implementation with normalized feedback "
            f"(attempt {attempt}/{max_attempts - 1}, scope={scope_label}) — "
            f"categories {result.category_counts()}"
        )
        feedback = build_contract_feedback(result, project_root=root, scope=scope)
        _invoke_rerun(rerun, feedback, scope)
        attempt += 1
        result = _run_oracle_command(root, profile, spec, config)

    if result.passed:
        echo(f"[greenfield] implement-oracle: {result.detail}")
    else:
        echo(
            f"[greenfield] implement-oracle: FAILED after {attempt} attempt(s) — "
            f"{result.detail}; categories {result.category_counts()}"
        )

    # Global orphan-artifact gate (invariant 1+2): after the typecheck loop, check
    # that every generated source artifact has an owning task. Default WARN
    # (observe + record on the result, never block); ENFORCE turns an orphan into a
    # failure. Only runs with a scope index (the owner map) and a real oracle.
    result = _apply_orphan_artifact_gate(
        result, project_root=root, scope_index=scope_index, manifest_paths=manifest_paths, config=config, echo=echo
    )
    return result


def _apply_orphan_artifact_gate(
    result: ImplementOracleResult,
    *,
    project_root: Path,
    scope_index: Any,
    manifest_paths: Any,
    config: Mapping[str, Any] | None,
    echo: Callable[[str], None],
) -> ImplementOracleResult:
    """Run the global orphan-artifact gate; return the (possibly failed) result.

    NO-OP unless a ``scope_index`` exists (no owner map ⇒ nothing to check) and the
    mode is not ``off``. WARN records the orphans on ``result.orphan_artifacts`` and
    logs them. ENFORCE additionally flips a passing result to a HARD failure (an
    orphan artifact is an out-of-contract file the SUT can mutate invisibly). The
    manifest/profile-owned files are treated as legitimately owned (the contract
    escape hatch). Best-effort: any failure to compute orphans is swallowed (the
    gate must never crash a build it was only observing).
    """
    if scope_index is None:
        return result
    mode = _orphan_artifact_gate_mode(config)
    if mode == "off":
        return result
    try:
        from codd.implement_oracle_scope import find_orphan_artifacts

        orphans = find_orphan_artifacts(
            scope_index, project_root, extra_owned=tuple(manifest_paths or ())
        )
    except Exception as exc:  # noqa: BLE001 — observation must not break the build.
        echo(f"[greenfield] implement-oracle: orphan-artifact gate skipped ({exc}).")
        return result
    if not orphans:
        return result

    paths = [o.path for o in orphans]
    result.orphan_artifacts = paths
    listing = ", ".join(paths[:_FEEDBACK_FINDING_CAP]) + (
        f", … (+{len(paths) - _FEEDBACK_FINDING_CAP} more)" if len(paths) > _FEEDBACK_FINDING_CAP else ""
    )
    if mode == "enforce":
        echo(
            f"[greenfield] implement-oracle: orphan-artifact gate (enforce) FAILED — "
            f"{len(paths)} generated artifact(s) own no task: {listing}. Every "
            f"artifact must be owned by a task (or declared harness/profile contract)."
        )
        if result.passed:
            # Flip an otherwise-clean result to a hard failure, carrying an honest
            # environment_build finding so the caller's StageError explains why.
            return ImplementOracleResult(
                passed=False,
                executed=result.executed,
                command=result.command,
                findings=[
                    ImplementOracleFinding(
                        category=EVIDENCE_ENVIRONMENT_BUILD,
                        code="orphan_artifact",
                        message=(
                            f"{len(paths)} generated artifact(s) own no task: {listing}"
                        ),
                    )
                ],
                detail=f"orphan-artifact gate (enforce): {len(paths)} unowned artifact(s)",
                raw_output=result.raw_output,
                diagnostics=result.diagnostics,
                orphan_artifacts=paths,
            )
        return result
    # WARN (default): observe + report, never block.
    echo(
        f"[greenfield] implement-oracle: orphan-artifact gate (warn) — "
        f"{len(paths)} generated artifact(s) own no task: {listing}. (Observation "
        f"only; set implement.orphan_artifact_gate: enforce to make this a hard gate.)"
    )
    return result


# Ladder-rung constants mirrored here so the gate does not hard-import the scope
# module at call time when no index is supplied (keeps the no-op path light).
_SCOPE_NARROW = "narrow"
_SCOPE_BROAD = "broad"


def _next_rung(rung: str) -> str | None:
    from codd.implement_oracle_scope import next_rung

    return next_rung(rung)


def _diagnostic_signature(result: ImplementOracleResult) -> tuple[Any, ...]:
    """The result's diagnostic signature (the loop-breaker key); () if none."""
    if not result.diagnostics:
        return ()
    try:
        from codd.implement_oracle_scope import diagnostic_signature

        return diagnostic_signature(result.diagnostics)
    except Exception:  # noqa: BLE001 — signature is a guard; a parse miss ⇒ no guard.
        return ()


#: How many earlier signatures the cycle detector remembers. Small — the attempt
#: budget is small, so only a TIGHT cycle (A↔B↔A) can recur within it; a longer
#: cycle drains the budget first (and the oscillation test catches its steps).
_SIGNATURE_HISTORY_WINDOW = 4


def _classify_progress(
    signature: tuple[Any, ...],
    last_signature: tuple[Any, ...] | None,
    history: list[tuple[Any, ...]],
) -> str:
    """Classify progress between signatures (delegates to the scope module).

    Best-effort: a classification failure degrades to ``stuck`` (escalate) — the
    safe default, never an infinite stay at one rung.
    """
    try:
        from codd.implement_oracle_scope import classify_signature_progress

        return classify_signature_progress(signature, last_signature, history=history)
    except Exception:  # noqa: BLE001 — a classify miss must escalate, not loop.
        from codd.implement_oracle_scope import PROGRESS_STUCK

        return PROGRESS_STUCK


def _escalation_decision(
    verdict: str,
    *,
    rung: str,
    soft_progress_used: set[str],
) -> tuple[bool, str]:
    """Map a progress verdict → ``(should_escalate, human_reason)``.

    * strict progress → stay (the repair is shrinking the SAME incoherence).
    * soft progress → stay ONCE per rung (record the allowance), then escalate.
    * oscillation / stuck / cycle → escalate immediately.

    The soft allowance is the ``narrow 2nd attempt only while making progress``
    budget the cap (default 5) is sized for: one extra narrow shot when the SUT is
    genuinely converging, but no thrashing.
    """
    from codd.implement_oracle_scope import (
        PROGRESS_CYCLE,
        PROGRESS_OSCILLATION,
        PROGRESS_SOFT,
        PROGRESS_STRICT,
        PROGRESS_STUCK,
    )

    if verdict == PROGRESS_STRICT:
        return False, "strict progress"
    if verdict == PROGRESS_SOFT:
        if rung in soft_progress_used:
            return True, "soft progress already spent its one allowance"
        soft_progress_used.add(rung)
        return False, "soft progress (one allowance)"
    if verdict == PROGRESS_OSCILLATION:
        return True, "diagnostics oscillating (not a shrink)"
    if verdict == PROGRESS_CYCLE:
        return True, "diagnostic cycle detected"
    # PROGRESS_STUCK or any unknown verdict → escalate (safe default).
    del PROGRESS_STUCK  # named for clarity; the fallthrough covers it
    return True, "signature unchanged"


def _derive_scope_for_rung(
    *,
    result: ImplementOracleResult,
    project_root: Path,
    scope_index: Any,
    rung: str,
    structured_source: Any,
    manifest_paths: Any,
    echo: Callable[[str], None],
    legacy_broad: bool = False,
) -> tuple[Any, bool]:
    """Derive the rerun scope for ``rung`` → ``(scope, forced_broad)``.

    ``scope is None`` ⇒ broad (the legacy signal the callback re-runs everything
    on). ``forced_broad`` is True when the derivation itself demanded broad (no
    determinable owner / too-wide / wide-fan-out artifact, or a derivation error)
    so the caller can PIN the ladder rung at broad — the ladder is monotonic, it
    never drops back to a narrower rung once broad was required. With no
    ``scope_index`` (the back-compat path) returns ``(None, False)`` so behaviour
    is exactly the legacy broad rerun without disturbing the rung bookkeeping.

    A wide-fan-out artifact (with ``legacy_broad`` False) yields a BROAD-CAMPAIGN
    scope (``scope.repair_plan`` set, ``scope.is_broad()`` True). The caller checks
    ``is_broad_campaign()`` and branches to ``_execute_broad_campaign`` instead of
    the single rerun+recheck. With ``legacy_broad`` True the wide-fan-out path falls
    to the legacy whole-project broad (``scope is None``, forced_broad True).
    """
    if scope_index is None:
        return None, False
    try:
        from codd.implement_oracle_scope import derive_oracle_rerun_scope

        decision = derive_oracle_rerun_scope(
            output=result.raw_output,
            project_root=project_root,
            index=scope_index,
            rung=rung,
            structured_source=structured_source,
            manifest_paths=tuple(manifest_paths or ()),
            legacy_broad=legacy_broad,
        )
    except Exception as exc:  # noqa: BLE001 — a derivation failure degrades to broad.
        echo(f"[greenfield] implement-oracle: scope derivation failed ({exc}); falling back to broad.")
        return None, True
    if decision.scope is None or decision.force_broad:
        if decision.reason:
            echo(f"[greenfield] implement-oracle: {decision.reason}")
        return None, True
    echo(f"[greenfield] implement-oracle: {decision.scope.detail}")
    # A broad-campaign scope is NOT a forced-broad-to-pin signal: the caller runs
    # the campaign and stays in control. Only the legacy broad rung pins.
    is_campaign = bool(getattr(decision.scope, "is_broad_campaign", lambda: False)())
    pin_broad = (decision.scope.rung == _SCOPE_BROAD) and not is_campaign
    return decision.scope, pin_broad


# ─────────────────────────────────────────────────────────────────────────────
# Broad repair campaign (the budgeted residual-coherence execution; GPT design §6)
# ─────────────────────────────────────────────────────────────────────────────


def _execute_broad_campaign(
    *,
    result: ImplementOracleResult,
    plan: Any,
    project_root: Path,
    profile: LayoutProfile,
    spec: ImplementOracleSpec,
    config: Mapping[str, Any] | None,
    rerun: Callable[..., None],
    scope_index: Any,
    structured_source: Any,
    manifest_paths: Any,
    echo: Callable[[str], None],
) -> ImplementOracleResult:
    """Run the budgeted residual-coherence campaign for a wide-fan-out broad rung.

    The campaign (GPT design §2/§6):

        result = whole_project_oracle()   # passed in (already failing)
        while result.failed and budget_left and rechecks < broad_max_rechecks:
            phase = next_phase(result)    # supplier_first → residual_importers
                                          #   (re-derived from residual diagnostics)
                                          #   → chunked_broad
            rerun_targeted_phase(phase)   # ONLY phase-scope tasks, fenced, with
                                          #   build_contract_feedback(scope=phase.scope)
            result = whole_project_oracle()   # ALWAYS the global authority
            if result.passed: return passed
            if no_progress_or_cycle(signature, phase): advance_phase_or_break

    ANTI-FALSE-GREEN INVARIANT (load-bearing): the WHOLE-PROJECT oracle is the ONLY
    green authority. A phase's local edit making the phase files typecheck proves
    NOTHING — green is returned ONLY when ``_run_oracle_command`` over the whole
    project passes. Budget exhaustion, non-convergence (the same (signature, phase,
    task_ids) recurring), or an exhausted phase skeleton → HONEST FAIL: return the
    still-failing ``result`` (the caller raises StageError). Best-effort campaign
    state is appended to ``.codd/oracle_repair/campaign.jsonl`` per phase (used ONLY
    for resume-efficiency + the human audit, NEVER for the green decision).
    """
    import time

    if plan is None:  # defensive: no plan ⇒ nothing to run, honest-fail unchanged
        return result

    budget = _oracle_broad_wall_clock_seconds(config)
    max_rechecks = _oracle_broad_max_rechecks(config)
    residual_chunk_size = _oracle_residual_chunk_size(config)
    started = time.monotonic()
    rechecks = 0
    completed_phases: list[str] = []
    #: (signature, phase, frozenset(task_ids)) tuples already EXECUTED — the
    #: loop-breaker: re-seeing one means the same edit on the same files produced the
    #: same residual (exporter↔importer oscillation / stuck) → do not retry it.
    executed_keys: set[tuple[Any, str, frozenset]] = set()
    any_changes = False

    def _elapsed() -> float:
        return time.monotonic() - started

    def _budget_left_for_phase() -> bool:
        remaining = budget - _elapsed()
        return remaining >= (_BROAD_MIN_CALL_BUDGET_SECONDS + _BROAD_ORACLE_RECHECK_RESERVE_SECONDS)

    supplier_ids = tuple(getattr(plan, "supplier_task_ids", ()) or ())

    while not result.passed and rechecks < max_rechecks:
        # Budget gate BEFORE the next AI phase: if too little wall-clock remains to
        # run a phase AND its verifying recheck, stop now and honest-fail (the
        # workspace changes persist for resume; never green).
        if not _budget_left_for_phase():
            _append_campaign_record(
                project_root,
                event="oracle_broad_budget_exhausted",
                phase="(none)",
                focus_paths=tuple(getattr(plan, "focus_paths", ()) or ()),
                task_ids=(),
                before_signature=_diagnostic_signature(result),
                after_signature=_diagnostic_signature(result),
                elapsed=_elapsed(),
                status="budget_exhausted",
                echo=echo,
            )
            echo(
                f"[greenfield] implement-oracle: broad campaign wall-clock budget "
                f"({budget:g}s) exhausted with residual diagnostics — honest failure "
                f"(no green; partial progress kept for resume)."
            )
            return result

        before_signature = _diagnostic_signature(result)
        phase, phase_scope = _next_campaign_phase(
            plan=plan,
            result=result,
            completed_phases=completed_phases,
            supplier_ids=supplier_ids,
            project_root=project_root,
            scope_index=scope_index,
            structured_source=structured_source,
            manifest_paths=manifest_paths,
            residual_chunk_size=residual_chunk_size,
        )
        if phase is None:
            echo(
                "[greenfield] implement-oracle: broad campaign exhausted its phases with "
                "residual diagnostics — honest failure (no green)."
            )
            return result

        task_ids = tuple(getattr(phase_scope, "task_ids", ()) or ())
        key = (before_signature, phase, frozenset(task_ids))
        if key in executed_keys:
            # The same edit on the same files already produced this residual — the
            # SUT is oscillating/stuck on this phase. Advance the phase; do not
            # retry. (If this was the last phase, the next loop's next_phase → None
            # → honest-fail.)
            echo(
                f"[greenfield] implement-oracle: broad campaign phase '{phase}' would "
                f"repeat the same (signature, task_ids) — not retrying; advancing phase."
            )
            if phase not in completed_phases:
                completed_phases.append(phase)
            _append_campaign_record(
                project_root,
                event="oracle_broad_phase_skipped_cycle",
                phase=phase,
                focus_paths=tuple(getattr(phase, "focus_paths", ()) or ())
                if hasattr(phase, "focus_paths")
                else (),
                task_ids=task_ids,
                before_signature=before_signature,
                after_signature=before_signature,
                elapsed=_elapsed(),
                status="cycle_no_retry",
                echo=echo,
            )
            continue

        executed_keys.add(key)

        # Re-implement ONLY this phase's tasks, fenced to the phase scope's allowed
        # paths, with contract feedback CARRYING the phase scope so the broad
        # subphase ALSO emits the minimal-diff / allowed-paths / exporter-surface
        # directives (today only scoped reruns get them).
        feedback = build_contract_feedback(result, project_root=project_root, scope=phase_scope)
        echo(
            f"[greenfield] implement-oracle: broad campaign phase '{phase}' — "
            f"{len(task_ids)} task(s) {list(task_ids)} "
            f"(elapsed {_elapsed():.0f}s / {budget:g}s, recheck {rechecks + 1}/{max_rechecks})."
        )
        phase_started = time.monotonic()
        _invoke_rerun(rerun, feedback, phase_scope)
        any_changes = True
        # supplier_first + chunked_broad are ONE-SHOT (design §5: supplier max-1 per
        # focus artifact; chunked broad max-1 pass) → mark complete so they never
        # re-run. residual_importers stays ELIGIBLE so it can repair the residual
        # chunk-by-chunk across rechecks; the (signature, phase, task_ids) loop-
        # breaker above advances it to chunked_broad once it stops making progress.
        from codd.implement_oracle_scope import PHASE_RESIDUAL_IMPORTERS as _PHASE_RESIDUAL

        if phase != _PHASE_RESIDUAL and phase not in completed_phases:
            completed_phases.append(phase)

        # ALWAYS re-run the WHOLE-PROJECT oracle — the only green authority.
        result = _run_oracle_command(project_root, profile, spec, config)
        rechecks += 1
        after_signature = _diagnostic_signature(result)
        phase_elapsed = time.monotonic() - phase_started

        status = "passed" if result.passed else ("progress" if after_signature != before_signature else "stuck")
        _append_campaign_record(
            project_root,
            event="oracle_broad_phase",
            phase=phase,
            focus_paths=tuple(getattr(phase_scope, "allowed_paths", ()) or ()),
            task_ids=task_ids,
            before_signature=before_signature,
            after_signature=after_signature,
            elapsed=phase_elapsed,
            status=status,
            echo=echo,
        )

        if result.passed:
            echo(
                f"[greenfield] implement-oracle: broad campaign converged after phase "
                f"'{phase}' — whole-project oracle PASSED (green)."
            )
            return result

    # Loop exit without a pass = honest failure (budget/recheck-cap/non-convergence).
    if rechecks >= max_rechecks and not result.passed:
        _append_campaign_record(
            project_root,
            event="oracle_broad_recheck_cap",
            phase="(none)",
            focus_paths=tuple(getattr(plan, "focus_paths", ()) or ()),
            task_ids=(),
            before_signature=_diagnostic_signature(result),
            after_signature=_diagnostic_signature(result),
            elapsed=_elapsed(),
            status="recheck_cap",
            echo=echo,
        )
        echo(
            f"[greenfield] implement-oracle: broad campaign hit the recheck cap "
            f"({max_rechecks}) without a clean whole-project oracle — honest failure."
        )
    del any_changes  # workspace changes already persisted by the reruns themselves
    return result


def _next_campaign_phase(
    *,
    plan: Any,
    result: ImplementOracleResult,
    completed_phases: list[str],
    supplier_ids: tuple[str, ...],
    project_root: Path,
    scope_index: Any,
    structured_source: Any,
    manifest_paths: Any,
    residual_chunk_size: int | None = None,
) -> tuple[str | None, Any]:
    """Pick the next campaign phase + its LIVE scope, or ``(None, None)`` if done.

    * ``supplier_first`` (if not yet done) → the plan's supplier phase scope
      (re-implement the shared exporter once).
    * ``residual_importers`` → a scope RE-DERIVED from the CURRENT residual
      diagnostics (the owner tasks the whole-project oracle STILL proves broken,
      minus the supplier already fixed). Falls back to the plan's static importer
      phase scope when the live derivation yields nothing but the static set is
      non-empty. If both are empty (no residual owner), skip to the next phase.
    * ``chunked_broad`` → the plan's all-tasks-dependency-order phase scope.

    Returns the phase NAME + the :class:`OracleRerunScope` to run.
    """
    from codd.implement_oracle_scope import (
        PHASE_CHUNKED_BROAD,
        PHASE_RESIDUAL_IMPORTERS,
        PHASE_SUPPLIER_FIRST,
        derive_residual_importer_scope,
    )

    # 1. supplier_first — at most once (the plan skeleton ensures the single phase).
    if PHASE_SUPPLIER_FIRST not in completed_phases:
        supplier_phase = _plan_phase(plan, PHASE_SUPPLIER_FIRST)
        if supplier_phase is not None and getattr(supplier_phase.scope, "task_ids", ()):
            return PHASE_SUPPLIER_FIRST, supplier_phase.scope
        completed_phases.append(PHASE_SUPPLIER_FIRST)  # nothing to fix here, skip

    # 2. residual_importers — re-derive from the LIVE residual, excluding the
    # supplier (avoid re-touching the exporter → no exporter↔importer oscillation).
    if PHASE_RESIDUAL_IMPORTERS not in completed_phases:
        live_scope = None
        if scope_index is not None:
            try:
                live_scope = derive_residual_importer_scope(
                    output=result.raw_output,
                    project_root=project_root,
                    index=scope_index,
                    exclude_task_ids=supplier_ids,
                    manifest_paths=tuple(manifest_paths or ()),
                    structured_source=structured_source,
                    chunk_size=residual_chunk_size,
                )
            except Exception:  # noqa: BLE001 — live derivation best-effort; fall to static.
                live_scope = None
        if live_scope is not None and getattr(live_scope, "task_ids", ()):
            return PHASE_RESIDUAL_IMPORTERS, live_scope
        static_phase = _plan_phase(plan, PHASE_RESIDUAL_IMPORTERS)
        if static_phase is not None and getattr(static_phase.scope, "task_ids", ()):
            return PHASE_RESIDUAL_IMPORTERS, static_phase.scope
        completed_phases.append(PHASE_RESIDUAL_IMPORTERS)  # no residual owner, skip

    # 3. chunked_broad — the last-resort full dependency-ordered pass.
    if PHASE_CHUNKED_BROAD not in completed_phases:
        chunked_phase = _plan_phase(plan, PHASE_CHUNKED_BROAD)
        if chunked_phase is not None and getattr(chunked_phase.scope, "task_ids", ()):
            return PHASE_CHUNKED_BROAD, chunked_phase.scope
        completed_phases.append(PHASE_CHUNKED_BROAD)

    return None, None


def _plan_phase(plan: Any, phase_name: str) -> Any:
    """The plan's :class:`OracleRepairPhase` with ``phase_name``, or ``None``."""
    for phase in getattr(plan, "phases", ()) or ():
        if getattr(phase, "phase", None) == phase_name:
            return phase
    return None


def _append_campaign_record(
    project_root: Path,
    *,
    event: str,
    phase: str,
    focus_paths: tuple[str, ...],
    task_ids: tuple[str, ...],
    before_signature: Any,
    after_signature: Any,
    elapsed: float,
    status: str,
    echo: Callable[[str], None],
) -> None:
    """Append one campaign event to ``.codd/oracle_repair/campaign.jsonl`` (best-effort).

    Records phase, focus_paths, task_ids, before/after signature, elapsed, status —
    used ONLY for resume-efficiency + the human audit, NEVER for the green decision.
    A write failure is swallowed (the campaign must never crash the run on an audit
    write) and only logged at debug-ish level.
    """
    try:
        audit_dir = Path(project_root) / ".codd" / "oracle_repair"
        audit_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "event": event,
            "phase": phase,
            "focus_paths": list(focus_paths),
            "task_ids": list(task_ids),
            "before_signature": _signature_to_jsonable(before_signature),
            "after_signature": _signature_to_jsonable(after_signature),
            "elapsed_seconds": round(float(elapsed), 3),
            "status": status,
        }
        with (audit_dir / "campaign.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — audit write must never break the run.
        echo(f"[greenfield] implement-oracle: campaign audit write skipped ({exc}).")


def _signature_to_jsonable(signature: Any) -> list:
    """Turn a diagnostic signature (tuple of tuples) into a JSON-serializable list."""
    try:
        return [list(entry) for entry in (signature or ())]
    except TypeError:
        return []


def _invoke_rerun(rerun: Callable[..., None], feedback: str, scope: Any) -> None:
    """Call the rerun callback, supporting both the scoped ``(feedback, scope)``
    and the legacy single-arg ``(feedback)`` signatures.

    Arity is decided by INSPECTION (not by catching ``TypeError``): a ``TypeError``
    raised *inside* a 2-arg callback must propagate, not be silently retried as a
    1-arg call. A callback that takes only one positional parameter (and no
    ``*args``) is invoked with feedback alone (it cannot localize → broad).
    """
    if _accepts_scope_arg(rerun):
        rerun(feedback, scope)
    else:
        rerun(feedback)


def _accepts_scope_arg(rerun: Callable[..., None]) -> bool:
    """True if ``rerun`` accepts a second positional (the scope) — else legacy."""
    import inspect

    try:
        sig = inspect.signature(rerun)
    except (TypeError, ValueError):
        return True  # un-introspectable (builtin/C) — assume the new signature.
    positional = 0
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
        elif param.kind is inspect.Parameter.VAR_POSITIONAL:
            return True  # *args swallows the scope.
    return positional >= 2


def _only_environment(result: ImplementOracleResult) -> bool:
    """True when every finding is an environment/toolchain failure (not curable by SUT)."""
    if not result.findings:
        return False
    return all(f.category == EVIDENCE_ENVIRONMENT_BUILD for f in result.findings)
