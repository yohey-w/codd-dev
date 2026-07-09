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

CONTRACT-DRIVEN (Contract Kernel oracle dispatch — not hardcoded)
=================================================================
EVERY supported language runs on the Contract-Kernel contract path: its
:class:`~codd.languages.profile.LanguageProfile` declares a modeled
``implement_oracle`` (kind + command-ids + adapter id) and the named oracle adapter
is REGISTERED. The dispatch resolves ``(profile, oracle decl, adapter)`` generically
(:func:`_resolve_contract_oracle`) and routes by the declaration's ``kind`` — Go
(``go-toolchain``, composite: ``typecheck`` + ``vet``) and TS (``typescript-tsc``,
command: ``tsc --noEmit``) through the generic command-sequence executor
(:func:`codd.languages.oracle_executor.run_command_sequence`), Python
(``python-composite``, adapter) through its in-process ``execute``. There is NO
hardcoded language-name comparison in this gate (Cut Condition A) — a new compiler
stack is one profile entry + one leaf oracle adapter, never a core edit here. A
stack with no modeled oracle + registered adapter makes the gate a strict NO-OP
(the verify-stage gates stay the backstop).

REUSE
=====
The tool semantics live behind the per-language oracle adapters
(:mod:`codd.languages.adapters.oracle_go` / ``oracle_python`` / ``oracle_typescript``);
the TS adapter ATTRIBUTES through the existing
``codd.repair.test_failure_attribution.attribute_command_failure`` (the same tsc
diagnostic parser verify uses). The dependency-install preflight is now LANGUAGE-FREE
(:func:`_run_materialize_preflight`): when an oracle command declares
``requires_materialized_deps`` it runs the profile's
``toolchain.package_manager.materialize_command`` (TS: ``npm ci``) BLOCKING before
the oracle — the generalization of the old node-install preflight (it no longer
hardwires ``codd.project_types.node_install_command``). This module owns the
implement-time PLACEMENT, the SCOPE-CERTIFICATION orchestration, and the STAGE-level
bounded-retry loop — it does not re-implement tsc running or normalization.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from codd.project_types import (
    ImplementOracleSpec,
    LayoutProfile,
    resolve_layout_profile,
    synthesize_implement_oracle_spec,
)

# Value-objects + evidence constants relocated to the LEAF module so the Contract
# Kernel oracle adapters (and the generic command-sequence executor) can import
# them without a cycle through this gate module. Re-imported + re-exported here so
# every existing ``from codd.implement_oracle import ImplementOracleResult`` keeps
# working and gets the SAME class object (identity preserved). See
# ``codd/implement_oracle_types.py`` — pure relocation, zero behaviour change.
from codd.implement_oracle_types import (  # noqa: F401 — re-exported for back-compat
    EVIDENCE_BOUNDARY_VIOLATION,
    EVIDENCE_CATEGORIES,
    EVIDENCE_ENVIRONMENT_BUILD,
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    EVIDENCE_OTHER,
    EVIDENCE_TEST_NOT_COLLECTED,
    ImplementOracleFinding,
    ImplementOracleResult,
    OracleScopeError,
    _FEEDBACK_FINDING_CAP,
)


__all__ = [
    "EVIDENCE_CATEGORIES",
    "ORACLE_STATE_LEGACY_ABSENT",
    "ORACLE_STATE_OPT_OUT",
    "ORACLE_STATE_SUPPORTED",
    "ORACLE_STATE_UNSUPPORTED_EXPLICIT",
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
    "classify_implement_oracle_state",
    "normalize_oracle_output",
    "normalize_python_tool_output",
    "resolve_implement_oracle",
    "run_implement_oracle_gate",
]

#: Internal-but-tested campaign entry point (the anti-false-green acceptance tests
#: drive it with a fake oracle + rerun). Not part of the stable public surface, but
#: importable for the gate's unit tests.
__test_exports__ = ["_execute_broad_campaign"]


# NOTE: the language-neutral evidence categories (``EVIDENCE_*`` +
# ``EVIDENCE_CATEGORIES``) now live in ``codd.implement_oracle_types`` (the leaf
# module the oracle adapters share) and are re-imported at the top of this module —
# see that import for the rationale (cycle-free adapter access).
#
# The TS-specific code→category MAPS, the tsc diagnostic regex, and the TS18003
# "no inputs" false-green guard regex were RELOCATED to the TS oracle adapter
# (:mod:`codd.languages.adapters.oracle_typescript`) with the Contract-Kernel TS
# switch (Contract Kernel oracle dispatch §7) — the gate no longer normalizes tsc
# output itself (TS routes through the generic command-sequence executor + the
# ``typescript-tsc`` adapter). The public ``normalize_oracle_output`` name below is
# kept as a DELEGATING SHIM to that adapter's parser (back-compat for existing
# importers + tests).


# NOTE: ``OracleScopeError`` / ``ImplementOracleFinding`` / ``ImplementOracleResult``
# (and the ``_FEEDBACK_FINDING_CAP`` its ``feedback_message`` uses) now live in
# ``codd.implement_oracle_types`` (the leaf the oracle adapters share) and are
# re-imported at the top of this module — see that import for the cycle-free
# rationale. Byte-for-byte the same definitions; only the home moved.


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


# ── the 4-state oracle dispatch model (Contract Kernel oracle dispatch §6, §9) ──
#
# When :func:`resolve_implement_oracle` returns ``None`` it means ONLY "no runnable
# oracle resolved" — it COLLAPSES four very different reasons into one signal. §9
# closes the false-green that collapse hid: a DECLARED-but-UNSUPPORTED stack (a
# non-empty language CoDD was told to build, with NO registered oracle adapter) was
# waved through as a silent NO-OP PASS. The gate must distinguish:
#
#   SUPPORTED            — a contract oracle resolved (run it; GREEN only if it
#                          actually passes). [resolved is not None]
#   UNSUPPORTED_EXPLICIT — a NON-EMPTY language is declared but NO oracle resolves
#                          AND not opted out → RED (passed=False, executed=False,
#                          code="implement_oracle_unsupported"). The §9 closure: a
#                          declared stack CoDD cannot prove must NOT pass.
#   LEGACY_ABSENT        — language is None/empty (NO declared stack) → NO-OP, but
#                          a VISIBLE fallback trace; NON-RED (nothing to be
#                          "unsupported" about — there is no declared stack).
#   OPT_OUT              — ``implement.implement_oracle: false`` → NO-OP-WITH-TRACE
#                          (``unsupported_oracle_allowed_by_config``), excluded from
#                          the green-gate; NON-RED by default (preserves the
#                          documented opt-out contract), never silent.
#
# Cardinal rule: false-GREEN is forbidden, but do NOT over-RED a legitimate
# no-oracle case (no language at all, or an explicit opt-out) — those are
# NO-OP-WITH-TRACE (visible), never silent, never RED.
#
# Language-FREE: the classification keys on "did a contract oracle resolve?" + "is
# a non-empty language declared?" + "is it opted out?" — NEVER on a specific
# language name (Cut Condition A). Adding a new compiler stack is still one profile
# entry + one leaf adapter; it then resolves SUPPORTED with no edit here.
ORACLE_STATE_SUPPORTED = "supported"
ORACLE_STATE_UNSUPPORTED_EXPLICIT = "unsupported_explicit"
ORACLE_STATE_LEGACY_ABSENT = "legacy_absent"
ORACLE_STATE_OPT_OUT = "opt_out"


def classify_implement_oracle_state(
    language: str | None,
    config: Mapping[str, Any] | None,
    *,
    resolved: object,
) -> str:
    """Classify the oracle dispatch state for ``language`` (the 4-state model, §9).

    ``resolved`` is the :func:`resolve_implement_oracle` result (a ``(profile, spec)``
    tuple, or ``None``). The classification is language-FREE — it keys on whether a
    runnable oracle resolved, whether a NON-EMPTY language is declared, and whether
    the gate is opted out — never on a language name (Cut Condition A):

    * ``resolved is not None``                → ``SUPPORTED``.
    * opted out                               → ``OPT_OUT`` (NO-OP-with-trace).
    * empty/None language                     → ``LEGACY_ABSENT`` (NO-OP-with-trace).
    * a SUPPORTED contract resolves anyway    → ``LEGACY_ABSENT`` (defensive: the
      resolver returned ``None`` for some OTHER reason than "unsupported"; never
      RED a stack that genuinely HAS an adapter — bound the blast radius).
    * otherwise (non-empty language, no contract, not opted out)
                                              → ``UNSUPPORTED_EXPLICIT`` (RED).

    The OPT_OUT check precedes the language-emptiness/contract checks because an
    opt-out on a SUPPORTED language must read as the (visible, non-RED) opt-out, not
    as supported — :func:`resolve_implement_oracle` already short-circuits opt-out to
    ``None`` before it ever looks at the profile.
    """
    if resolved is not None:
        return ORACLE_STATE_SUPPORTED
    if _oracle_opt_out(config):
        return ORACLE_STATE_OPT_OUT
    if (language or "").strip() == "":
        return ORACLE_STATE_LEGACY_ABSENT
    # A non-empty language with no resolved (profile, spec). If a contract oracle
    # nonetheless resolves (a registered adapter exists), the resolver returned None
    # for an unrelated reason — do NOT RED a stack CoDD CAN actually prove; treat it
    # as a (visible) legacy-absent NO-OP. Only a TRULY unsupported stack (no contract)
    # is the §9 RED.
    if _resolve_contract_oracle(language) is not None:
        return ORACLE_STATE_LEGACY_ABSENT
    return ORACLE_STATE_UNSUPPORTED_EXPLICIT


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


def _dependency_boundary_gate_enabled(config: Mapping[str, Any] | None) -> bool:
    """``implement.dependency_boundary_gate`` — default ON (the gate runs).

    The deterministic source dependency-conformance gate (Increment 1). Opting out
    (``implement.dependency_boundary_gate: false`` / ``off``) re-opens the silent
    design↔code boundary-drift a project could otherwise green past, so it is never
    the default and never silent. An unrecognized value keeps the gate ON.
    """
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "dependency_boundary_gate" in section:
        raw = section["dependency_boundary_gate"]
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() not in {"off", "false", "0", "no", "disabled"}
    return True


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


# NOTE: the per-command oracle timeout knob (``implement.oracle_timeout_seconds``)
# is now read by the generic command-sequence executor
# (:func:`codd.languages.oracle_executor._oracle_timeout_seconds`) — the gate no
# longer spawns the oracle command itself (TS/Go/Python all run on the contract
# path), so its local timeout helper was removed with the legacy ``_run_oracle_
# command`` body. The default constant is kept for documentation + back-compat.

#: tsc on a fresh build is fast, but a cold first run can compile a large graph;
#: a generous-but-bounded budget. Override via ``implement.oracle_timeout_seconds``.
#: (Consumed by the generic executor; the executor declares the same default.)
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

    # (3) Dependency-boundary DUAL — when the failure carries source dependency-
    # boundary violations (Increment 1), the rerun MUST be told the two legitimate
    # resolutions and forbidden the code-duplication dodge (Fable5 Q2). Independent
    # of the finding cap so the directive is never truncated away.
    boundary_block = _boundary_violation_block(result)
    if boundary_block:
        blocks.append(boundary_block)

    return "\n\n".join(blocks)


def _boundary_violation_block(result: ImplementOracleResult) -> str:
    """The dependency-boundary DUAL directive block (empty when no such finding).

    States the two — and only two — legitimate fixes for a source file that
    imports across its owning design doc's declared ``depends_on`` closure, and
    forbids the anti-false-green dodge of inlining/duplicating the code to avoid
    the boundary (Fable5 ``verify-coherence`` Q2, the "dual").
    """
    boundary = [f for f in result.findings if f.category == EVIDENCE_BOUNDARY_VIOLATION]
    if not boundary:
        return ""
    lines = [
        "DEPENDENCY-BOUNDARY VIOLATION(S): a generated SOURCE file imports across a "
        "DECLARED dependency boundary — the imported module is owned by a design "
        "doc that is NOT the importer's own doc and NOT in that doc's transitive "
        "`depends_on` closure. Reconcile EACH by the dual rule (do ONE of these):",
        "  1. Import the needed capability from a design doc that is ALREADY a "
        "declared dependency (in the `depends_on` closure) and provides it; OR",
        "  2. If NO declared dependency provides it, this is a DESIGN-level gap (a "
        "missing `depends_on` edge). Do NOT inline or duplicate the code to dodge "
        "the boundary — the correct fix is a declared dependency, not a copy.",
    ]
    for finding in boundary[:_FEEDBACK_FINDING_CAP]:
        where = f"{finding.path}: " if finding.path else ""
        lines.append(f"  - {where}{finding.message}")
    return "\n".join(lines)


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


# ── configured-root unify (Cut A.3 integration boundary — the source_root DIMENSION) ──
#
# THE INVARIANT (anti-false-green, cardinal): the strict v3 oracle must observe the
# SAME resolved tree that GENERATION routes to. Generation routes by the resolved
# ``LayoutProfile`` (``codd.project_types.resolve_layout_profile`` →
# ``source_root`` / ``package_root`` / ``test_root`` from ``scan.source_dirs`` /
# ``scan.test_dirs``; ``greenfield.pipeline._route_source_into_package`` writes the
# generated tree there). With ``scan: {source_dirs: ["lib"], test_dirs: ["spec"]}``
# the code lands in ``lib/<pkg>`` + ``spec``. The v3 oracle adapter, however, reads
# its roots from ``ctx.language_profile.layout`` (the registry ``LayoutSpec`` — for
# Python the FIXED ``src/{package_name}`` + ``tests``), so WITHOUT this unify it would
# certify + execute against ``src/<pkg>`` + ``tests`` while the actually-generated
# ``lib/<pkg>`` + ``spec`` is unverified — a "proved the wrong tree" false-green (the
# GPT-5.5 round-2 cross-check blocker for v3.0.0).
#
# THE FIX (Option A — project-resolve, drift impossible BY CONSTRUCTION): rebase the
# registry ``LayoutSpec`` to the SAME resolved ``LayoutProfile`` generation used, so
# the oracle's source/test/package roots are a FUNCTION of the same resolved roots —
# they cannot diverge. The adapter stays a pure ``LayoutSpec`` + ``package_name``
# reader (NO legacy ``source_root``, NO bridge revival — the Cut A.3 lock holds): only
# the LayoutSpec it reads is now derived from the configured layout instead of the
# fixed default. The rebase KEEPS the ``{package_name}`` template (the adapter still
# substitutes + hard-fails on an unresolved name). The COMMON case (scan unset, or
# scan == the default ``src``/``tests``) rebases to a LayoutSpec that is byte-identical
# to the registry one (behavior-preserving — no false-RED). A BELT cross-check then
# asserts the rebased oracle roots equal the generation-routed roots, hard-failing
# (never silently) if a future change reintroduced drift.


def _rebase_layout_spec_to_profile(layout: Any, profile: "LayoutProfile") -> Any:
    """Rebase a ``LayoutSpec`` so its template roots reflect the resolved ``LayoutProfile``.

    ``profile`` is the SAME ``LayoutProfile`` generation routing uses (resolved from
    ``scan.source_dirs`` / ``scan.test_dirs`` via :func:`resolve_layout_profile`): its
    ``source_root`` / ``package_root`` / ``test_root`` are the (substituted) roots the
    generated tree lands in. This returns a ``LayoutSpec`` whose single source-set
    root, ``package_root.path``, and single (first) test-set root reflect those roots —
    while PRESERVING the ``{package_name}`` template (so the adapter's substitution +
    unresolved-name HARD FAIL are unchanged) and every other set/field.

    Mapping by ``package_root.kind``:
      * ``named_package`` (Python ``src/{package_name}``): the source-set + package_root
        path become ``<profile.source_root>/{package_name}`` — the adapter then derives
        ``source_root`` = its parent (``profile.source_root``) and ``package_root`` =
        ``<profile.source_root>/<package_name>`` = ``profile.package_root``. The test
        root becomes ``profile.test_root``.
      * ``path_root`` (TS ``src``, ``package_root == source_root``): the source-set +
        package_root path become ``profile.source_root``; the test root becomes
        ``profile.test_root``.
      * ``none`` (Go — no single package root, no scan-driven src layout): returned
        UNCHANGED (Go routes to the repo root; ``scan.*_dirs`` does not relocate it).

    Single-root only by intent: only ``source_sets[0]`` / ``test_sets[0]`` are rebased
    (the legacy ``LayoutProfile`` is single-root, and Python/TS declare exactly one
    source-set + one unit test-set). Any extra sets are carried verbatim — the
    single-source / single-required-test HARD FAIL in the adapter still fires for >1.
    """
    from dataclasses import replace

    pkg = getattr(layout, "package_root", None)
    pkg_kind = getattr(pkg, "kind", "none")
    if pkg_kind == "none":
        # Root-module language (Go): no single package root, no scan-driven relocation.
        return layout

    source_root = _norm(getattr(profile, "source_root", "") or "")
    test_root = _norm(getattr(profile, "test_root", "") or "")
    if not source_root or not test_root:
        # An incomplete legacy profile — leave the registry LayoutSpec untouched and let
        # the adapter's own scope certification decide (never guess a relocated root).
        return layout

    if pkg_kind == "named_package":
        new_pkg_path = f"{source_root}/{{package_name}}"
        new_source_root_template = f"{source_root}/{{package_name}}"
    else:  # path_root
        new_pkg_path = source_root
        new_source_root_template = source_root

    source_sets = tuple(getattr(layout, "source_sets", ()) or ())
    test_sets = tuple(getattr(layout, "test_sets", ()) or ())

    def _rebase_globs(old_root: str, new_root: str, globs: tuple[str, ...]) -> tuple[str, ...]:
        """Re-anchor each glob from the old root prefix to the new one (keep the tail).

        ``src/{package_name}/**/*.py`` rebased ``src/{package_name}`` → ``lib/{package_name}``
        becomes ``lib/{package_name}/**/*.py``. A glob not under the old root is carried
        verbatim (defensive — only the leading topology changed). The adapter reads
        ``root`` (not globs) for Python, but keeping globs consistent matters for other
        consumers (TS/coverage) and avoids a stale-prefix glob surviving a rebase.
        """
        out: list[str] = []
        old_n = _norm(old_root)
        new_n = _norm(new_root)
        for g in globs:
            g_n = str(g).replace("\\", "/")
            if old_n and (g_n == old_n or g_n.startswith(old_n + "/")):
                out.append(new_n + g_n[len(old_n):])
            else:
                out.append(g)
        return tuple(out)

    new_source_sets = source_sets
    if source_sets:
        s0 = source_sets[0]
        new_source_sets = (
            replace(
                s0,
                root=new_source_root_template,
                file_globs=_rebase_globs(
                    s0.root, new_source_root_template, tuple(getattr(s0, "file_globs", ()) or ())
                ),
            ),
        ) + source_sets[1:]
    new_test_sets = test_sets
    if test_sets:
        t0 = test_sets[0]
        new_test_sets = (
            replace(
                t0,
                root=test_root,
                file_globs=_rebase_globs(
                    t0.root, test_root, tuple(getattr(t0, "file_globs", ()) or ())
                ),
            ),
        ) + test_sets[1:]

    return replace(
        layout,
        source_sets=new_source_sets,
        test_sets=new_test_sets,
        package_root=replace(pkg, path=new_pkg_path),
    )


def _oracle_language_profile(lang_profile: Any, profile: "LayoutProfile | None") -> Any:
    """The resolved ``LanguageProfile`` whose ``.layout`` the oracle adapter observes.

    Returns ``lang_profile`` with its ``LayoutSpec`` rebased to the SAME resolved
    ``LayoutProfile`` generation routed to (so the oracle certifies + executes against
    the SAME tree — anti-false-green: never a different tree than generation). When
    ``profile`` is ``None`` (a synthetic carrier from :func:`_resolve_registry_oracle`
    for a language with no legacy ``LayoutProfile`` — Go) the registry LayoutSpec is
    returned unchanged (nothing to rebase; the scan knob does not relocate it).

    The COMMON case rebases to a byte-identical LayoutSpec (scan unset / == default →
    the same template roots), so the adapter's derivation is unchanged there.
    """
    if profile is None:
        return lang_profile
    from dataclasses import replace

    rebased = _rebase_layout_spec_to_profile(lang_profile.layout, profile)
    if rebased is lang_profile.layout:
        return lang_profile
    return replace(lang_profile, layout=rebased)


def _certify_oracle_observes_generation_tree(
    ctx: Any, oracle_decl: Any, profile: "LayoutProfile | None"
) -> None:
    """BELT (defense in depth): hard-fail if the tree the oracle will observe differs
    from the tree generation routes to.

    Option A rebases the oracle's ``LayoutSpec`` FROM the same resolved ``LayoutProfile``
    generation used, so the two agree BY CONSTRUCTION; this tripwire converts ANY future
    reintroduction of drift into a HARD FAIL (``OracleScopeError``) — never a silent
    "proved the wrong tree" green. It compares the roots the adapter would derive from
    ``ctx`` against the generation-routed ``profile`` roots.

    ``None`` profile (the synthetic carrier for a no-legacy-LayoutProfile language — Go)
    has nothing to cross-check (Go routes to the repo root; the scan knob does not
    relocate it). A non-named-package (``path_root``/``none``) layout has no single
    ``package_root`` to compare, so only source/test roots are checked. An incomplete
    legacy profile (missing a root) defers to the adapter's own scope certification.
    """
    if profile is None:
        return
    gen_source = _norm(getattr(profile, "source_root", "") or "")
    gen_test = _norm(getattr(profile, "test_root", "") or "")
    gen_package = _norm(getattr(profile, "package_root", "") or "")
    if not (gen_source and gen_test):
        return  # incomplete legacy profile — the adapter's own certification governs

    layout = getattr(getattr(ctx, "language_profile", None), "layout", None)
    if layout is None:
        return
    pkg = getattr(layout, "package_root", None)
    pkg_kind = getattr(pkg, "kind", "none")
    if pkg_kind == "none":
        return  # root-module language (Go) — no scan-driven relocation to cross-check

    source_sets = tuple(getattr(layout, "source_sets", ()) or ())
    test_sets = tuple(getattr(layout, "test_sets", ()) or ())
    if not source_sets or not test_sets:
        return  # the adapter's scope certification will hard-fail an empty required set

    package_name = getattr(ctx, "package_name", None)

    def _subst(template: str) -> str:
        rendered = str(template)
        if "{package_name}" in rendered and package_name:
            rendered = rendered.replace("{package_name}", str(package_name))
        return _norm(rendered)

    obs_package = _subst(getattr(pkg, "path", "") or "")
    # Python's ``named_package`` source root is the PARENT of the package dir; a
    # ``path_root`` (TS) has source_root == package_root.
    if pkg_kind == "named_package":
        obs_source = _norm(obs_package.rsplit("/", 1)[0]) if "/" in obs_package else "."
    else:
        obs_source = _subst(source_sets[0].root)
    obs_test = _subst(test_sets[0].root)

    mismatch = obs_source != gen_source or obs_test != gen_test
    if pkg_kind == "named_package" and gen_package:
        mismatch = mismatch or obs_package != gen_package
    if mismatch:
        raise OracleScopeError(
            "implement-time oracle ABORTED to avoid a false-green: the tree the oracle "
            f"would observe (source={obs_source!r}, package={obs_package!r}, "
            f"test={obs_test!r}) DIFFERS from the tree generation routes to "
            f"(source={gen_source!r}, package={gen_package!r}, test={gen_test!r}). The "
            "strict v3 oracle must observe the SAME resolved tree generation/scaffold "
            "routes to (a green over a different tree proves nothing — the cardinal "
            "anti-false-green rule). This is a HARD FAIL, never a silent pass."
        )


# NOTE: ``_glob_covers_root`` (the tsconfig include/files coverage test) was
# relocated to the TS oracle adapter (Contract Kernel oracle dispatch §7) — the
# gate's ``certify_oracle_scope`` now delegates scope certification to the registered
# adapter, so the gate no longer parses tsconfig globs itself.


def certify_oracle_scope(
    project_root: Path,
    profile: LayoutProfile,
    spec: ImplementOracleSpec,  # noqa: ARG001 — kept for the stable gate signature.
) -> str:
    """Certify the oracle's scope covers source + tests, else raise OracleScopeError.

    Returns a human-readable certification detail on success; raises
    :class:`OracleScopeError` when the scope cannot be proven to cover source +
    tests — a green oracle over an unknown / partial / empty scope is a false-green
    (the #1 design failure mode: "tests outside compile scope = false green").

    CONTRACT PATH (Contract Kernel oracle dispatch §5–§7): EVERY supported language
    certifies through its registered oracle adapter's ``certify_scope`` — Go
    (``go-toolchain``: missing go.mod / undecidable module path / empty module → RED),
    Python (``python-composite``: an empty required source/test root → RED), and TS
    (``typescript-tsc``: missing tsconfig.json / test root excluded → RED). The
    selection is GENERIC — modeled oracle + registered adapter via
    :func:`_resolve_contract_oracle` — with NO hardcoded language-name comparison
    (Cut Condition A; the legacy in-gate tsc certifier moved to the TS adapter).

    Cut A.3: the ``OracleContext.layout_profile`` is ALWAYS the resolved ``LayoutSpec``
    (``lang_profile.layout``) — NO legacy ``LayoutProfile`` layout-VIEW override. A
    ``kind="adapter"`` language (Python) derives its ``source_root``/``test_root`` from
    the ``LayoutSpec`` (``source_sets``/``package_root`` template) + the gate-resolved
    ``package_name`` carried on the context. A command-sequence oracle (Go reads
    ``module_root``; TS reads ``source_sets``/``test_sets``) reads the SAME ``LayoutSpec``.
    """
    contract = _resolve_contract_oracle(profile.language)
    if contract is None:
        # No modeled oracle + registered adapter for this language. The gate only
        # reaches certification for a resolved oracle spec, so this is an unsupported
        # stack reaching the certifier — RED, never a silent "certified" (anti-false-
        # green: a green over an uncertifiable/unknown scope proves nothing).
        raise OracleScopeError(
            f"implement-time oracle cannot be certified for language {profile.language!r}: "
            "no registered oracle adapter to certify its scope. A green oracle over an "
            "uncertified scope would be a false-green (HARD FAIL)."
        )
    lang_profile, oracle_decl, adapter = contract
    from codd.languages.adapters.implement_oracle import OracleContext

    # Cut A.3 integration boundary (the source_root DIMENSION): rebase the registry
    # LayoutSpec to the SAME resolved LayoutProfile generation routed to, so the oracle
    # certifies the tree the generated code actually lands in (configured layout =
    # lib/<pkg> + spec, not the fixed src/<pkg> + tests) — never a different tree
    # (anti-false-green). Common case (scan unset / == default) rebases byte-identically.
    oracle_lang_profile = _oracle_language_profile(lang_profile, profile)
    ctx = OracleContext(
        project_root=project_root,
        layout_profile=oracle_lang_profile.layout,
        language_profile=oracle_lang_profile,
        oracle=oracle_decl,
        config=None,
        package_name=getattr(profile, "package_name", None),
    )
    # BELT: the tree the oracle will observe MUST equal the generation-routed tree
    # (Option A makes them agree by construction; this hard-fails on any future drift).
    _certify_oracle_observes_generation_tree(ctx, oracle_decl, profile)
    return adapter.certify_scope(ctx)


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` and ``/* */`` comments so a JSONC tsconfig parses as JSON.

    DELEGATING SHIM → :func:`codd.languages.adapters.oracle_typescript._strip_jsonc`
    (the body relocated to the TS adapter with the Contract-Kernel TS switch). Kept
    here because :mod:`codd.implement_oracle_scope` still imports
    ``from codd.implement_oracle import _strip_jsonc`` (its tsconfig path-scope
    reader). Same byte-for-byte behaviour; only the home moved.
    """
    from codd.languages.adapters.oracle_typescript import _strip_jsonc as _impl

    return _impl(text)


# ── evidence normalization — RELOCATED to the TS oracle adapter ──
#
# The tsc diagnostic normalizer (the ``_TS_*`` code maps + the per-line parser + the
# TS18003 "no inputs" guard + ``_diag_path``) moved to
# :mod:`codd.languages.adapters.oracle_typescript` with the Contract-Kernel TS switch
# (Contract Kernel oracle dispatch §7). TS now normalizes through that adapter's
# ``normalize_command_result`` on the generic command-sequence path. The public
# ``normalize_oracle_output`` name is kept as a DELEGATING SHIM (back-compat for
# existing importers + the TS unit tests).


def normalize_oracle_output(
    output: str,
    *,
    command: str,  # noqa: ARG001 — kept for the stable signature (attribution is path-based).
    project_root: Path,
    profile: LayoutProfile = None,  # noqa: ARG001 — kept for the stable signature; unused.
) -> tuple[list[ImplementOracleFinding], list[str]]:
    """Normalize raw tsc output → (findings, editable failed paths).

    DELEGATING SHIM → the relocated TS parser
    (:func:`codd.languages.adapters.oracle_typescript._parse_ts_diagnostics`). Same
    semantics: every ``error TSxxxx: message`` line → an
    :class:`ImplementOracleFinding` with a language-neutral category; a ``No inputs
    were found`` (TS18003) → an ``environment_build_error`` finding; the editable
    ``failed_paths`` resolved through the SAME tsc attribution the verify stage uses.
    The bodies moved to the adapter with the Contract-Kernel TS switch; the behaviour
    did not.
    """
    from codd.languages.adapters.oracle_typescript import _parse_ts_diagnostics

    return _parse_ts_diagnostics(output or "", project_root)


# ═════════════════════════════════════════════════════════════════════════════
# Python composite implement-oracle — RELOCATED to the Contract-Kernel adapter
# (Contract Kernel oracle dispatch §6, the PYTHON SWITCH). The whole stack (the
# 3-layer in-process composite + scope certifier + value objects + regexes) now
# lives in :mod:`codd.languages.adapters.oracle_python` as
# ``PythonCompositeOracleAdapter`` (a ``kind="adapter"`` oracle). Python's profile
# (``codd/languages/profiles/python.yaml``) declares ``implement_oracle: {kind:
# adapter, adapter: python-composite}``; once that adapter is REGISTERED (see
# ``register_oracle_adapters``) the gate's ``kind="composite"`` dispatch resolves
# Python to the contract path and runs the adapter's ``execute(ctx)``. The gate no
# longer branches on the Python language literal at all (Cut Condition A: the
# forbidden-zone grep for a Python language-name comparison is empty in this gate).
#
# DELEGATING SHIMS (back-compat): the two public ``__all__`` names
# (``certify_python_oracle_scope`` / ``normalize_python_tool_output``), the
# directly-imported value object ``PythonToolRun`` / ``PythonOracleScope``, and the
# unit-locked ``_collection_failure_is_third_party_only`` re-export from the leaf
# adapter so every existing ``from codd.implement_oracle import ...`` keeps working
# and gets the SAME object. Pure re-export — the bodies moved, the behaviour did not.
# ═════════════════════════════════════════════════════════════════════════════

from codd.languages.adapters.oracle_python import (  # noqa: E402,F401 — relocated, re-exported for back-compat
    PythonOracleScope,
    PythonToolRun,
    _collection_failure_is_third_party_only,
    _run_python_pytest_collect_layer,
    certify_python_oracle_scope,
    normalize_python_tool_output,
)


def certify_go_oracle_scope(
    project_root: Path,
    profile: LayoutProfile,
    spec: ImplementOracleSpec,  # noqa: ARG001 — signature parity with the TS/Python certifiers.
) -> str:
    """Back-compat shim → the ``go-toolchain`` adapter's ``certify_scope``.

    The Go scope semantics moved to
    :meth:`codd.languages.adapters.oracle_go.GoToolchainOracleAdapter.certify_scope`
    (Contract Kernel oracle dispatch §5). This thin shim preserves the legacy
    ``(project_root, LayoutProfile, ImplementOracleSpec)`` signature some callers/
    tests still use: it builds the contract :class:`OracleContext` from the real Go
    :class:`~codd.languages.profile.LanguageProfile`, overriding ``module_root`` with
    the LayoutProfile's ``source_root`` (the legacy module-root carrier), and
    delegates. Same anti-false-green hard-fails (missing go.mod / undecidable module
    path / empty module → :class:`OracleScopeError`).
    """
    from dataclasses import replace

    from codd.languages import default_registry
    from codd.languages.adapters.implement_oracle import OracleContext
    from codd.languages.adapters.oracle_go import GoToolchainOracleAdapter

    lang_profile = default_registry.resolve(profile.language)
    module_root = _norm(getattr(profile, "source_root", "") or "") or "."
    layout = replace(lang_profile.layout, module_root=module_root)
    ctx = OracleContext(
        project_root=project_root,
        layout_profile=layout,
        language_profile=lang_profile,
        oracle=lang_profile.implement_oracle,
        config=None,
    )
    return GoToolchainOracleAdapter().certify_scope(ctx)


# ═════════════════════════════════════════════════════════════════════════════
# Contract-path oracle dispatch (Contract Kernel oracle dispatch §3–§7).
#
# A language whose resolved ``LanguageProfile`` declares a modeled
# ``implement_oracle`` AND whose oracle adapter is REGISTERED runs on the
# Contract-Kernel contract path instead of a hand-written per-language executor:
# the generic :func:`codd.languages.oracle_executor.run_command_sequence` spawns
# the profile's command sequence (Go: ``typecheck`` + ``vet``; TS: ``typecheck``)
# and an :class:`~codd.languages.adapters.implement_oracle.ImplementOracleAdapter`
# (Go: ``go-toolchain``; TS: ``typescript-tsc``) certifies scope + normalizes each
# command's output. Python (``python-composite``) runs in-process via the adapter's
# ``execute`` instead of a command sequence.
#
# ALL THREE built-in stacks (Go / Python / TS) are now registered, so every one
# takes this path — there is NO per-language executor left in the gate. The
# selection predicate is GENERIC — "modeled oracle + registered adapter" — never a
# hardcoded language-name comparison (Cut Condition A; the oracle's finish line).
# ═════════════════════════════════════════════════════════════════════════════


def _resolve_contract_oracle(
    language: str | None,
) -> "tuple[Any, Any, Any] | None":
    """Resolve ``(LanguageProfile, ImplementOracleProfileSpec, adapter)`` or ``None``.

    Returns the contract-path triple when ``language`` resolves to a
    :class:`~codd.languages.profile.LanguageProfile` that declares a modeled
    ``implement_oracle`` whose ``adapter`` is REGISTERED under
    ``("implement_oracle", adapter_id)``; otherwise ``None`` (no contract oracle —
    an unsupported stack the gate treats as a NO-OP / RED depending on the caller).
    ALL THREE built-in stacks now resolve: Go (``go-toolchain``), Python
    (``python-composite``), TS (``typescript-tsc``) — the registered-adapter gate is
    the GENERIC selection predicate (Cut Condition A: no language-name comparison).

    ``language`` is the RUNTIME-provided stack language (an alias like ``golang``
    resolves via the registry's case-insensitive id/alias lookup). Best-effort: a
    registry/profile error degrades to ``None`` (never a crash; the verify-stage
    gates stay the backstop).
    """
    lang = (language or "").strip()
    if not lang:
        return None
    try:
        from codd.languages import default_registry
        from codd.languages.builtin_adapters import ensure_builtin_adapters_registered
        from codd.languages.contract import KIND_IMPLEMENT_ORACLE
        from codd.languages.registry import default_adapter_registry

        lang_profile = default_registry.resolve(lang)
    except Exception:  # noqa: BLE001 — no registry profile ⇒ no contract oracle, not a crash.
        return None
    oracle_decl = getattr(lang_profile, "implement_oracle", None)
    if oracle_decl is None:
        return None
    # Gate on the REGISTERED adapter. ``ensure_builtin_…`` is idempotent; it registers
    # all three built-in oracle adapters (go-toolchain / python-composite /
    # typescript-tsc) on first use, so every built-in stack resolves to its contract.
    try:
        ensure_builtin_adapters_registered(default_adapter_registry)
        adapter = default_adapter_registry.get(KIND_IMPLEMENT_ORACLE, oracle_decl.adapter)
    except Exception:  # noqa: BLE001 — registration hiccup ⇒ legacy path.
        return None
    if adapter is None:
        return None
    return lang_profile, oracle_decl, adapter


def _contract_oracle_command_ids(oracle_decl: Any) -> list[str]:
    """The command-id sequence for a modeled oracle declaration.

    ``composite`` → every step's command id; ``command`` → the single command id.
    An ``adapter``-kind oracle has no shell command sequence (it would run via the
    adapter's ``execute``) — not used on the contract command-sequence path here.
    """
    kind = getattr(oracle_decl, "kind", None)
    if kind == "composite":
        return [step.command for step in oracle_decl.steps]
    if kind == "command" and oracle_decl.command:
        return [oracle_decl.command]
    return []


def _oracle_requires_materialized_deps(lang_profile: Any, oracle_decl: Any) -> bool:
    """True iff ANY of the oracle's command steps declares ``requires_materialized_deps``.

    Reads the resolved :class:`CommandSpec` for each oracle command id from
    ``lang_profile.commands``. Language-free: the flag (not a language name) decides
    whether the install preflight runs. Go's ``typecheck`` sets it False (the Go
    oracle tolerates uninstalled third-party deps under ``-mod=readonly``) → Go skips
    the preflight; TS's ``typecheck`` sets it True (tsc needs ``node_modules``) → TS
    runs it. An ``adapter``-kind oracle (Python) has no command ids → False (its
    in-process composite tolerates uninstalled third-party deps too).
    """
    commands = getattr(lang_profile, "commands", {}) or {}
    for command_id in _contract_oracle_command_ids(oracle_decl):
        spec = commands.get(command_id)
        if spec is not None and bool(getattr(spec, "requires_materialized_deps", False)):
            return True
    return False


def _materialize_command(lang_profile: Any) -> tuple[list[str], str | None] | None:
    """The profile's ``toolchain.package_manager.materialize_command`` → (argv, cwd).

    Returns ``None`` when the profile declares no materialize command (then there is
    nothing to install — the caller proceeds without a preflight). The command lives
    in the loose ``package_manager`` mapping as ``{argv: [...], cwd: "{module_root}"}``
    (TS: ``npm ci``). ``cwd`` may carry layout placeholders (resolved by the caller).
    """
    toolchain = getattr(lang_profile, "toolchain", None)
    if toolchain is None:
        return None
    pm = getattr(toolchain, "package_manager", None) or {}
    raw = pm.get("materialize_command") if hasattr(pm, "get") else None
    if not raw or not hasattr(raw, "get"):
        return None
    argv = list(raw.get("argv") or [])
    if not argv:
        return None
    cwd = raw.get("cwd")
    return [str(a) for a in argv], (str(cwd) if cwd is not None else None)


def _run_materialize_preflight(
    project_root: Path,
    lang_profile: Any,
    oracle_decl: Any,
    config: Mapping[str, Any] | None,
) -> ImplementOracleResult | None:
    """Blocking dependency install before a contract oracle whose steps need deps.

    The language-free generalization of the legacy ``_run_node_install`` (which was
    hardwired to ``node_install_command``): when an oracle command declares
    ``requires_materialized_deps`` AND the profile declares a materialize command,
    run it BLOCKING so the oracle tool (tsc) has its ``node_modules`` before it runs.
    Returns ``None`` when no preflight is needed (no requiring step, or no declared
    materialize command). An install FAILURE / TIMEOUT is an honest
    ``environment_build_error`` :class:`ImplementOracleResult` (passed=False,
    executed=True, NOT retryable) — exactly like the legacy node-install preflight, so
    a build/toolchain failure is never handed to the SUT to "fix" in source.

    Reuses the legacy install's timeout knob (``implement.oracle_install_timeout_
    seconds`` via :func:`_install_timeout`) and spawns through this module's
    ``subprocess.run`` (the SAME seam the legacy preflight + the existing TS tests
    mock). ``argv`` comes from the trusted language profile; run ``shell=False``.
    """
    if not _oracle_requires_materialized_deps(lang_profile, oracle_decl):
        return None
    resolved = _materialize_command(lang_profile)
    if resolved is None:
        # A step needs deps but the profile declares no installer: there is nothing to
        # run as a preflight. The oracle tool's own ``--no-install`` / readonly mode
        # then surfaces a missing dependency as an honest finding (never a silent green
        # synthesized here).
        return None
    argv, cwd_template = resolved
    from codd.languages.verify_plan import _substitute_layout_placeholders

    layout = getattr(lang_profile, "layout", None)
    cwd_value = (
        _substitute_layout_placeholders(cwd_template, layout)
        if (cwd_template and layout is not None)
        else cwd_template
    )
    run_cwd = (project_root / cwd_value) if cwd_value and cwd_value not in (".", "") else project_root
    command_str = " ".join(argv)
    timeout = _install_timeout(config)
    try:
        completed = subprocess.run(  # noqa: S603 — trusted argv from the profile, shell=False
            argv,
            shell=False,
            cwd=str(run_cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ImplementOracleResult(
            passed=False,
            executed=True,
            command=command_str,
            findings=[
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="install_timeout",
                    message=f"dependency install exceeded {timeout:g}s",
                )
            ],
            detail=f"dependency install timed out after {timeout:g}s: {command_str}",
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return ImplementOracleResult(
            passed=False,
            executed=True,
            command=command_str,
            findings=[
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="install_spawn_error",
                    message=f"could not run dependency install ({exc}): {command_str}",
                )
            ],
            detail=f"could not run dependency install ({exc}): {command_str}",
        )
    if completed.returncode != 0:
        tail = _output_tail(completed.stdout, completed.stderr)
        return ImplementOracleResult(
            passed=False,
            executed=True,
            command=command_str,
            findings=[
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="install_failed",
                    message=f"dependency install failed (exit {completed.returncode})",
                )
            ],
            detail=f"dependency install failed (exit {completed.returncode}): {command_str}\n{tail}",
            raw_output=tail,
        )
    return None


def _run_contract_oracle(
    project_root: Path,
    lang_profile: Any,
    oracle_decl: Any,
    adapter: Any,
    config: Mapping[str, Any] | None,
    *,
    package_name: str | None = None,
    layout_profile: "LayoutProfile | None" = None,
) -> ImplementOracleResult:
    """Run a modeled oracle on the contract path (certify already done by caller).

    Builds the :class:`~codd.languages.adapters.implement_oracle.OracleContext`
    carrying the REAL resolved ``LanguageProfile`` (so the generic executor reads
    each command's argv/cwd/env from ``language_profile.commands`` — no hardcoded
    per-language registry re-resolution), then dispatches per the declaration's
    ``kind``:

    * ``composite`` / ``command`` → :func:`run_command_sequence` over the command
      ids (Go's ``typecheck`` + ``vet``); the adapter normalizes each step.
    * ``adapter`` → the registered adapter's ``execute`` (an in-process composite —
      Python's compile + first-party-import resolver + ``pytest --collect-only``; a
      declared ``kind="adapter"`` with no ``execute`` is an incomplete contract →
      RED, never a silent pass).

    Cut A.3: ``OracleContext.layout_profile`` is ALWAYS ``lang_profile.layout`` (the
    resolved ``LayoutSpec``) — there is NO legacy ``LayoutProfile`` layout-VIEW
    override. A ``kind="adapter"`` language whose layout template carries
    ``{package_name}`` (Python's ``src/{package_name}``) SUBSTITUTES the gate-resolved
    ``package_name`` carried on the context; Go's command-sequence adapter reads
    ``module_root`` from the same ``LayoutSpec`` (``package_name`` is ``None`` for it).
    """
    from codd.languages.adapters.implement_oracle import (
        OracleContext,
        adapter_supports_execute,
    )
    from codd.languages.oracle_executor import run_command_sequence

    # Cut A.3 integration boundary (the source_root DIMENSION): rebase the registry
    # LayoutSpec to the SAME resolved LayoutProfile generation routed to, so the oracle
    # EXECUTES against the tree the generated code actually lands in (anti-false-green:
    # never a different tree than generation). Common case rebases byte-identically.
    oracle_lang_profile = _oracle_language_profile(lang_profile, layout_profile)
    ctx = OracleContext(
        project_root=project_root,
        layout_profile=oracle_lang_profile.layout,
        language_profile=oracle_lang_profile,
        oracle=oracle_decl,
        config=config,
        package_name=package_name,
    )
    # BELT: the tree the oracle will observe MUST equal the generation-routed tree
    # (Option A makes them agree by construction; this hard-fails on any future drift).
    _certify_oracle_observes_generation_tree(ctx, oracle_decl, layout_profile)

    # Install preflight (language-free, BEFORE dispatch so run_command_sequence stays
    # pure): if ANY oracle step's resolved CommandSpec requires materialized deps AND
    # the profile declares a materialize command, run it BLOCKING (the generalization
    # of the legacy node-install preflight). An install FAILURE is a non-retryable
    # environment_build_error. Go opts out (requires_materialized_deps=False on its
    # typecheck) so it never runs ``go mod download`` here — no regression.
    install_failure = _run_materialize_preflight(project_root, lang_profile, oracle_decl, config)
    if install_failure is not None:
        return install_failure

    if getattr(oracle_decl, "kind", None) == "adapter":
        if not adapter_supports_execute(adapter):
            return ImplementOracleResult(
                passed=False,
                executed=False,
                command=f"{lang_profile.id}-oracle (adapter={oracle_decl.adapter})",
                findings=[
                    ImplementOracleFinding(
                        category=EVIDENCE_ENVIRONMENT_BUILD,
                        code="oracle_adapter_missing_execute",
                        message=(
                            f"oracle adapter {oracle_decl.adapter!r} declares kind='adapter' "
                            "but provides no execute() — an incomplete contract (RED, never "
                            "a silent pass)."
                        ),
                    )
                ],
                detail=f"kind='adapter' oracle {oracle_decl.adapter!r} has no executor",
            )
        return adapter.execute(ctx)

    command_ids = _contract_oracle_command_ids(oracle_decl)
    return run_command_sequence(ctx, command_ids, adapter)


# ── command execution (the Contract-Kernel contract path) ──


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
    spec: ImplementOracleSpec,  # noqa: ARG001 — kept for the stable gate signature.
    config: Mapping[str, Any] | None,
) -> ImplementOracleResult:
    """Run the native oracle ONCE on the Contract-Kernel contract path.

    CONTRACT PATH (Contract Kernel oracle dispatch §3–§7): EVERY supported language
    runs through the generic contract path — its resolved ``LanguageProfile`` declares
    a modeled ``implement_oracle`` whose adapter is REGISTERED, and the dispatch routes
    by the declaration's ``kind`` (no hardcoded language-name comparison — Cut
    Condition A):

    * Go (``go-toolchain``, ``kind="composite"``) → :func:`run_command_sequence` over
      its ``typecheck`` + ``vet`` commands.
    * TS (``typescript-tsc``, ``kind="command"``) → :func:`run_command_sequence` over
      the single ``typecheck`` command (``tsc --noEmit``). The install preflight
      (``npm ci``) runs inside :func:`_run_contract_oracle` first (tsc needs deps).
    * Python (``python-composite``, ``kind="adapter"``) → the registered adapter's
      in-process ``execute`` (compile + first-party import resolver + ``pytest
      --collect-only``).

    Cut A.3: the ``OracleContext.layout_profile`` is ALWAYS the resolved ``LayoutSpec``
    (``lang_profile.layout``) — the legacy ``LayoutProfile`` layout-VIEW override is
    retired. The Python adapter derives its ``source_root`` / ``test_root`` /
    ``package_root`` from the ``LayoutSpec`` (``source_sets`` / ``test_sets`` /
    ``package_root`` template) + the gate-resolved ``package_name`` (carried on the
    context, not a synthesized legacy profile). A command-sequence oracle (Go reads
    ``module_root``; TS reads ``source_sets``/``test_sets``) reads the SAME ``LayoutSpec``.

    No contract resolves (an unsupported stack reaching here) → an honest
    ``environment_build_error`` RED, never a silent pass (anti-false-green).
    """
    contract = _resolve_contract_oracle(profile.language)
    if contract is None:
        return ImplementOracleResult(
            passed=False,
            executed=False,
            command=f"{profile.language}-oracle",
            findings=[
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="oracle_unsupported",
                    message=(
                        f"no registered implement-oracle adapter for language "
                        f"{profile.language!r}; cannot run the oracle (RED, never a "
                        "silent pass)."
                    ),
                )
            ],
            detail=f"no contract oracle for language {profile.language!r}",
        )
    lang_profile, oracle_decl, adapter = contract
    # Cut A.3: carry the gate-resolved canonical ``package_name`` (the legacy profile
    # resolved it via ``resolve_canonical_package_name``) so a ``kind="adapter"`` oracle
    # whose layout template carries ``{package_name}`` (Python's ``src/{package_name}``)
    # can SUBSTITUTE it — instead of handing the adapter a legacy LayoutProfile layout
    # view. No override mechanism; the layout authority is the LayoutSpec.
    return _run_contract_oracle(
        project_root, lang_profile, oracle_decl, adapter, config,
        package_name=getattr(profile, "package_name", None),
        layout_profile=profile,
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

    CONTRACT-PATH languages (the language→oracle map entry): a language with no
    legacy ``LayoutProfile`` builder (Go — ``resolve_layout_profile('go')`` returns
    None) but a modeled ``implement_oracle`` whose adapter is REGISTERED resolves
    via :func:`_resolve_registry_oracle`, which synthesizes a minimal
    ``(LayoutProfile, ImplementOracleSpec)`` for ANY oracle ``kind`` (composite /
    command / adapter) so the existing gate machinery (certify/run/retry) works
    unchanged; the SAME dispatch in :func:`_run_oracle_command` then routes it to the
    Contract-Kernel contract path (:func:`_run_contract_oracle` →
    ``run_command_sequence`` for command/composite, the adapter's ``execute`` for
    ``adapter``). The selection is GENERIC (modeled oracle + registered adapter) — no
    ``if language=='go'`` literal in the gate, and NO kind allowlist (a synthetic
    ``kind="command"``/``"adapter"`` language with no legacy ``LayoutProfile`` runs
    its oracle too, never silently passes — Contract Kernel oracle dispatch §8).
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
        # No legacy LayoutProfile — try the declarative registry. ANY kind resolves
        # (Go's composite, a synthetic command/adapter language): the generic
        # synthesizer builds the minimal gate-machinery ``(LayoutProfile, spec)`` and
        # the kind-routed dispatch runs the registered adapter (no kind allowlist).
        return _resolve_registry_oracle(language)
    if profile.implement_oracle is None:
        return None
    return profile, profile.implement_oracle


def _resolve_registry_oracle(
    language: str | None,
) -> tuple[LayoutProfile, ImplementOracleSpec] | None:
    """Synthesize a gate-machinery ``(LayoutProfile, spec)`` from the registry, or None.

    De-literalized + de-kind-allowlisted (Contract Kernel oracle dispatch §5 + §8):
    BOTH the old hardcoded ``{"go", "golang"}`` language allowlist AND the old
    ``kind == "composite"`` ONLY gate are gone. A language resolves to a synthesized
    ``(LayoutProfile, spec)`` IFF :func:`_resolve_contract_oracle` returns a triple —
    i.e. a modeled ``implement_oracle`` (ANY kind: composite / command / adapter)
    whose adapter is REGISTERED — and it has NO legacy ``LayoutProfile`` builder (so it
    reached here). The synthesized ``spec.kind`` MIRRORS the real declaration so the
    SAME kind-routed dispatch in :func:`_run_oracle_command` runs the registered
    adapter (composite/command → ``run_command_sequence``; adapter → the adapter's
    ``execute``) — no kind allowlist, so a synthetic ``kind="command"``/``"adapter"``
    language runs its oracle instead of falling to a silent NO-OP (the gap §8 closes).

    Cut A.3: the synthetic ``LayoutProfile`` is ONLY the gate's SPEC CARRIER (it
    carries ``language`` for the contract re-resolve, ``package_name`` for the
    context's ``{package_name}`` substitution, and the ``implement_oracle`` spec the
    certify/run/retry machinery reads) — it is NEVER a layout AUTHORITY. The adapter
    reads the real ``LayoutSpec`` off ``ctx.language_profile.layout`` (the
    ``synthesize_minimal_layout_view`` compat-view bridge is retired):

    * ``command`` / ``composite`` (Go: ``go-toolchain``) — the command-sequence
      executor reads cwd/env from ``lang_profile.layout`` (module root etc.); the
      synthetic ``LayoutProfile``'s ``source_root`` field is a vestigial carrier the
      adapter does not read.
    * ``adapter`` (a Python-style in-process composite with no legacy profile) — the
      adapter derives its source/test/package roots from ``ctx.language_profile.layout``
      (``source_sets``/``test_sets``/``package_root``) + ``ctx.package_name``; the
      synthetic profile only carries ``package_name`` + the spec.

    In practice Go is the only BUILT-IN that reaches here (TS/Python HAVE legacy
    ``LayoutProfile`` builders); the command/adapter branches exist so a NEW language
    (the §8 synthetic-language proof) is addable with NO core change. ``None`` for any
    language with neither a legacy profile nor a contract oracle (a strict NO-OP — step
    9 will make a profile-present-but-oracle-absent case explicit RED; untouched here).
    Best-effort: a registry error degrades to NO-OP (never a crash).
    """
    contract = _resolve_contract_oracle(language)
    if contract is None:
        return None
    lang_profile, oracle_decl, _adapter = contract
    # SHARED spec construction (drift-proofing): build the ImplementOracleSpec via the
    # SAME helper the synthesized LayoutProfile uses
    # (``codd.project_types.synthesize_implement_oracle_spec``), so a stack that LATER
    # gains a synthesized profile (csharp) keeps byte-identical oracle behaviour whether
    # it reaches the oracle via the legacy-profile path or this registry path.
    spec = synthesize_implement_oracle_spec(lang_profile)
    if spec is None:
        return None
    kind = getattr(oracle_decl, "kind", None)
    if kind == "adapter":
        # An in-process ``kind="adapter"`` oracle reads the LayoutSpec off the resolved
        # LanguageProfile (Cut A.3 — no synthesized layout view). The synthetic profile
        # is only the gate's spec carrier: ``language`` (contract re-resolve) +
        # ``package_name`` (the context's ``{package_name}`` substitution) + the spec.
        # The topology fields are required by the dataclass but VESTIGIAL here (the
        # adapter never reads them — it uses ``ctx.language_profile.layout``); fill them
        # from the LayoutSpec's first source-set so the carrier stays well-formed.
        _src_sets = tuple(getattr(lang_profile.layout, "source_sets", ()) or ())
        _carrier_root = _norm(_src_sets[0].root) if _src_sets else "src"
        synthetic = LayoutProfile(
            language=lang_profile.id,
            package_name=lang_profile.id,
            source_root=_carrier_root,
            package_root=_carrier_root,
            test_root="tests",
            implement_oracle=spec,
        )
        return synthetic, spec
    # command / composite: a command-sequence oracle. Carry the module root for the
    # commands' cwd; the executor reads argv/cwd/env from lang_profile.layout.
    module_root = _norm(getattr(lang_profile.layout, "module_root", ".") or ".") or "."
    synthetic = LayoutProfile(
        language=lang_profile.id,  # canonical id ("go") — carried for the contract re-resolve
        package_name=lang_profile.id,
        source_root=module_root,  # carries the module root for the go commands' cwd
        package_root=module_root,
        test_root=module_root,
        implement_oracle=spec,
    )
    return synthetic, spec


def _with_implement_oracle(
    profile: LayoutProfile, spec: ImplementOracleSpec
) -> LayoutProfile:
    """Return ``profile`` with its ``implement_oracle`` set to ``spec`` (frozen-safe)."""
    from dataclasses import replace

    return replace(profile, implement_oracle=spec)


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
        # No runnable oracle resolved — but WHY decides the verdict (§9). The 4-state
        # model: a DECLARED-but-UNSUPPORTED stack is RED (never a silent pass); no
        # declared stack OR an explicit opt-out is a NO-OP-WITH-TRACE (visible, non-RED).
        state = classify_implement_oracle_state(language, config, resolved=resolved)
        if state == ORACLE_STATE_UNSUPPORTED_EXPLICIT:
            # The §9 closure: a non-empty language CoDD was told to build, with NO
            # registered oracle adapter. CoDD cannot prove this stack's coherence, so it
            # must NOT pass — an explicit unsupported RED, never a silent NO-OP.
            echo(
                f"[greenfield] implement-oracle: language {language!r} is declared but "
                "UNSUPPORTED (no registered oracle adapter) — RED (a declared stack CoDD "
                "cannot prove must not silently pass)."
            )
            return ImplementOracleResult(
                passed=False,
                executed=False,
                command=f"{language}-oracle",
                findings=[
                    ImplementOracleFinding(
                        category=EVIDENCE_ENVIRONMENT_BUILD,
                        code="implement_oracle_unsupported",
                        message=(
                            f"no registered implement-oracle adapter for declared language "
                            f"{language!r}; the implement-time coherence oracle cannot run, so "
                            "the generated code's cross-artifact coherence is UNPROVEN. A "
                            "declared-but-unsupported stack is RED, never a silent pass "
                            "(add a LanguageProfile + oracle adapter for this stack, or opt "
                            "out explicitly via implement.implement_oracle: false)."
                        ),
                    )
                ],
                detail=(
                    f"declared-but-unsupported language {language!r}: no implement-time "
                    "oracle adapter (unsupported → RED)"
                ),
            )
        if state == ORACLE_STATE_OPT_OUT:
            # The user explicitly opted out (implement.implement_oracle: false). NOT RED
            # by default (preserves the documented opt-out contract; bounds blast radius),
            # but the NO-OP is now VISIBLE + excluded from the release green-gate.
            # [GPT §6 notes a stricter "RED in strict/greenfield" reading; we default to
            # NO-OP-with-trace so opt-out is never silently broken. To make opt-out RED in
            # strict mode instead, this branch would emit the unsupported RED above.]
            echo(
                "[greenfield] implement-oracle: opted out by config "
                "(implement.implement_oracle=false) — skipped, excluded from the green-gate "
                "(unsupported_oracle_allowed_by_config=true). NOT a proof."
            )
            return ImplementOracleResult(
                passed=True,
                executed=False,
                command="",
                detail=(
                    "implement-time oracle opted out by config "
                    "(implement.implement_oracle=false) — skipped, excluded from the "
                    "green-gate (unsupported_oracle_allowed_by_config=true)"
                ),
            )
        # LEGACY_ABSENT: no declared stack (language None/empty). Nothing to be
        # "unsupported" about — a passing NO-OP, but VISIBLE (a fallback, not a proof),
        # never silent.
        echo(
            "[greenfield] implement-oracle: no language declared — oracle skipped "
            "(fallback, not a proof)."
        )
        return ImplementOracleResult(
            passed=True,
            executed=False,
            command="",
            detail=f"no language declared — implement-time oracle skipped (fallback) [{language!r}]",
        )
    profile, spec = resolved

    # 2. Certify scope — HARD FAIL on an uncertifiable scope (raises). Scope
    # certification reads files from disk (tsconfig.json / go.mod / the .py list); it
    # never needs materialized deps, so it precedes the install. The BLOCKING
    # dependency install (for a contract oracle whose steps require materialized deps —
    # TS's ``tsc`` needs ``node_modules``) now runs INSIDE the contract path
    # (:func:`_run_contract_oracle` → :func:`_run_materialize_preflight`), the
    # language-free generalization of the legacy node-install preflight. An install
    # failure surfaces there as a non-retryable ``environment_build_error``.
    certification = certify_oracle_scope(root, profile, spec)
    echo(f"[greenfield] implement-oracle: {certification}")

    # 3. Run + bounded retry-with-feedback, escalating the rerun scope.
    max_attempts = _oracle_max_attempts(config)
    result = _run_oracle_command(root, profile, spec, config)
    # Deterministic source dependency-boundary gate (Increment 1): a SIBLING to the
    # orphan gate, applied after EACH oracle run so a boundary violation feeds the
    # bounded rerun loop below (fixing the impl's imports BEFORE verify runs).
    result = _apply_dependency_boundary_gate(
        result,
        project_root=root,
        language=language,
        project_name=project_name,
        config=config,
        profile=profile,
        echo=echo,
    )
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
        # Re-prove the dependency boundary against the just-reran sources so the
        # loop keeps iterating until the imports conform (or the budget is spent).
        result = _apply_dependency_boundary_gate(
            result,
            project_root=root,
            language=language,
            project_name=project_name,
            config=config,
            profile=profile,
            echo=echo,
        )

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


def _apply_dependency_boundary_gate(
    result: ImplementOracleResult,
    *,
    project_root: Path,
    language: str | None,
    project_name: str | None,
    config: Mapping[str, Any] | None,
    profile: LayoutProfile | None,
    echo: Callable[[str], None],
) -> ImplementOracleResult:
    """Merge deterministic source dependency-boundary violations into ``result``.

    A SIBLING to :func:`_apply_orphan_artifact_gate` (the precedent for a language-
    free gate in the oracle), applied right after each oracle command run so its
    findings FEED the bounded rerun loop: a resolved internal import to a design
    doc PROVABLY outside its owning doc's declared ``depends_on`` closure becomes an
    :data:`EVIDENCE_BOUNDARY_VIOLATION` finding, flips the result to failed (only a
    curable, non-environment failure — the loop reruns it), and the SUT is told the
    dual fix (declared dependency vs. design-level gap; never inline/duplicate).

    Anti-false-green / anti-false-RED (the Python oracle's "PROVABLY absent → fail;
    unknown → never fail" rule): unresolvable specifiers and undecidable closures
    degrade to logged residue, never a failure; the SOURCE-only v1 scope excludes
    (and LOGS) test-tree artifacts. NO-OP when opted out, when there are no derived
    tasks, or when nothing violates. Best-effort — a computation error is swallowed
    (the gate must never crash a build it is only proving).
    """
    if not _dependency_boundary_gate_enabled(config):
        return result
    try:
        from codd.dependency_boundary_coherence import check_dependency_boundary_coherence

        boundary = check_dependency_boundary_coherence(
            project_root,
            language=language,
            project_name=project_name,
            config=config,
            profile=profile,
        )
    except Exception as exc:  # noqa: BLE001 — proving must not break the build.
        echo(f"[greenfield] implement-oracle: dependency-boundary gate skipped ({exc}).")
        return result

    # Residue + the source-only exclusion are LOGGED (no silent cap), never a fail.
    if boundary.residue:
        shown = ", ".join(boundary.residue[:_FEEDBACK_FINDING_CAP])
        extra = len(boundary.residue) - _FEEDBACK_FINDING_CAP
        suffix = f", … (+{extra} more)" if extra > 0 else ""
        echo(
            f"[greenfield] implement-oracle: dependency-boundary residue "
            f"({len(boundary.residue)} unresolved/undecidable edge(s), never a "
            f"failure): {shown}{suffix}"
        )
    if boundary.excluded_test_artifacts:
        shown = ", ".join(boundary.excluded_test_artifacts[:_FEEDBACK_FINDING_CAP])
        extra = len(boundary.excluded_test_artifacts) - _FEEDBACK_FINDING_CAP
        suffix = f", … (+{extra} more)" if extra > 0 else ""
        echo(
            f"[greenfield] implement-oracle: dependency-boundary scope=source-only, "
            f"excluded {len(boundary.excluded_test_artifacts)} test artifact(s) "
            f"(covered by test_import_coherence): {shown}{suffix}"
        )
    if not boundary.findings:
        return result

    boundary_findings = [
        ImplementOracleFinding(
            category=EVIDENCE_BOUNDARY_VIOLATION,
            code="dependency_boundary_violation",
            message=finding.message,
            path=finding.path,
        )
        for finding in boundary.findings
    ]
    echo(
        f"[greenfield] implement-oracle: dependency-boundary gate FAILED — "
        f"{len(boundary_findings)} generated source import(s) cross a declared "
        f"dependency boundary."
    )

    merged_failed_paths = list(result.failed_paths)
    for finding in boundary.findings:
        if finding.path not in merged_failed_paths:
            merged_failed_paths.append(finding.path)
    boundary_detail = f"{len(boundary_findings)} dependency-boundary violation(s)"
    detail = f"{result.detail}; {boundary_detail}" if result.detail else boundary_detail
    return ImplementOracleResult(
        passed=False,
        executed=result.executed,
        command=result.command,
        findings=list(result.findings) + boundary_findings,
        failed_paths=merged_failed_paths,
        detail=detail,
        raw_output=result.raw_output,
        diagnostics=result.diagnostics,
        orphan_artifacts=result.orphan_artifacts,
    )


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
