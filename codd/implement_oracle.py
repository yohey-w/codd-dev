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

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from codd.project_types import (
    ImplementOracleSpec,
    LayoutProfile,
    OracleScopeSpec,
    node_install_command,
    resolve_layout_profile,
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


# NOTE: the language-neutral evidence categories (``EVIDENCE_*`` +
# ``EVIDENCE_CATEGORIES``) now live in ``codd.implement_oracle_types`` (the leaf
# module the oracle adapters share) and are re-imported at the top of this module —
# see that import for the rationale (cycle-free adapter access). The TS-specific
# code→category MAPS below stay here (they are TS normalizer internals, not shared
# value-objects).


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

    CONTRACT PATH (Contract Kernel oracle dispatch §5–§6): a contract-path language
    (modeled oracle + registered adapter — Go via ``go-toolchain``, Python via
    ``python-composite``) certifies through its adapter's ``certify_scope``: Go
    raises ``OracleScopeError`` on a missing go.mod / undecidable module path / empty
    module; Python certifies a CONCRETE file-list (an empty required source/test root
    is a HARD FAIL — a green oracle over an empty scope proves nothing). Generic
    predicate — no hardcoded language-name comparison here. For a ``kind="adapter"``
    language (Python) the legacy ``LayoutProfile`` is handed to the adapter as the
    layout VIEW (it reads source_root/test_root from it; the ``LayoutSpec`` does not
    carry those); Go reads ``module_root`` from the ``LayoutSpec`` (no override).
    """
    if spec.kind == "composite":
        contract = _resolve_contract_oracle(profile.language)
        if contract is not None:
            lang_profile, oracle_decl, adapter = contract
            from codd.languages.adapters.implement_oracle import OracleContext

            layout_view = (
                profile if getattr(oracle_decl, "kind", None) == "adapter" else lang_profile.layout
            )
            ctx = OracleContext(
                project_root=project_root,
                layout_profile=layout_view,
                language_profile=lang_profile,
                oracle=oracle_decl,
                config=None,
            )
            return adapter.certify_scope(ctx)
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

    This is the TS-path normalizer (the only language still on the legacy
    ``_run_oracle_command`` path). Python normalizes its ``pytest --collect-only``
    output via :func:`normalize_python_tool_output` (the public shim, called by the
    relocated ``python-composite`` adapter) — the composite oracle's compile +
    import-resolver layers build their findings in-process (no text to normalize), so
    a Python oracle never reaches this function.
    """
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
# Contract-path oracle dispatch (Contract Kernel oracle dispatch §3–§5).
#
# A language whose resolved ``LanguageProfile`` declares a modeled
# ``implement_oracle`` AND whose oracle adapter is REGISTERED runs on the
# Contract-Kernel contract path instead of a hand-written per-language executor:
# the generic :func:`codd.languages.oracle_executor.run_command_sequence` spawns
# the profile's command sequence (Go: ``typecheck`` + ``vet``) and an
# :class:`~codd.languages.adapters.implement_oracle.ImplementOracleAdapter`
# (Go: ``go-toolchain``) certifies scope + normalizes each command's output.
#
# Step 5 registers ONLY ``go-toolchain``, so Go takes this path; Python/TS keep
# their legacy ``LayoutProfile``-builder path until their adapters register
# (steps 6–7). The selection predicate is GENERIC — "modeled oracle + registered
# adapter" — never a hardcoded language-name comparison (Cut Condition A).
# ═════════════════════════════════════════════════════════════════════════════


def _resolve_contract_oracle(
    language: str | None,
) -> "tuple[Any, Any, Any] | None":
    """Resolve ``(LanguageProfile, ImplementOracleProfileSpec, adapter)`` or ``None``.

    Returns the contract-path triple when ``language`` resolves to a
    :class:`~codd.languages.profile.LanguageProfile` that declares a modeled
    ``implement_oracle`` whose ``adapter`` is REGISTERED under
    ``("implement_oracle", adapter_id)``; otherwise ``None`` (the caller stays on
    the legacy path). The registered-adapter gate is what makes ONLY Go migrate
    today — Python/TS resolve a profile but their oracle adapters are not yet
    registered, so this returns ``None`` for them.

    ``language`` is the RUNTIME-provided stack language (an alias like ``golang``
    resolves via the registry's case-insensitive id/alias lookup). Best-effort: a
    registry/profile error degrades to ``None`` (never a crash; the legacy path /
    verify-stage gates stay the backstop).
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
    except Exception:  # noqa: BLE001 — no registry profile ⇒ legacy path, not a crash.
        return None
    oracle_decl = getattr(lang_profile, "implement_oracle", None)
    if oracle_decl is None:
        return None
    # Gate on the REGISTERED adapter: a modeled oracle whose adapter is not yet
    # registered (Python/TS until steps 6–7) stays on legacy. ``ensure_builtin_…``
    # is idempotent; it registers the built-ins (incl. go-toolchain) on first use.
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


def _run_contract_oracle(
    project_root: Path,
    lang_profile: Any,
    oracle_decl: Any,
    adapter: Any,
    config: Mapping[str, Any] | None,
    *,
    layout_override: Any = None,
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

    ``layout_override`` lets a ``kind="adapter"`` language hand the adapter a RICHER
    layout VIEW than the profile's ``LayoutSpec``: Python's ``python-composite``
    reads ``source_root`` / ``test_root`` / ``package_root`` / ``package_name`` (the
    legacy Python ``LayoutProfile`` carries them; ``LayoutSpec`` does not). Go's
    command-sequence adapter still reads ``module_root`` from ``lang_profile.layout``
    (``layout_override`` is ``None`` for it). The ``OracleContext.layout_profile``
    field is duck-typed (the generic executor only reads ``module_root``/cwd/env from
    it on the command-sequence path, which the adapter path does not take).
    """
    from codd.languages.adapters.implement_oracle import (
        OracleContext,
        adapter_supports_execute,
    )
    from codd.languages.oracle_executor import run_command_sequence

    ctx = OracleContext(
        project_root=project_root,
        layout_profile=layout_override if layout_override is not None else lang_profile.layout,
        language_profile=lang_profile,
        oracle=oracle_decl,
        config=config,
    )

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

    CONTRACT PATH (Contract Kernel oracle dispatch §3–§6): a language whose
    resolved ``LanguageProfile`` declares a modeled ``implement_oracle`` with a
    REGISTERED adapter runs on the contract path instead of a hand-written
    executor. Go (``go-toolchain``, ``kind="composite"``) and Python
    (``python-composite``, ``kind="adapter"``) both qualify, so their legacy
    ``kind="composite"`` gate spec routes here: Go to
    :func:`run_command_sequence` over its ``typecheck`` + ``vet`` commands, Python
    to the registered adapter's in-process ``execute`` (compile + first-party
    import resolver + ``pytest --collect-only``). TS (``kind="compiler"``) keeps the
    legacy ``tsc`` path below (its adapter is not yet registered). The predicate is
    GENERIC (modeled oracle + registered adapter via :func:`_resolve_contract_oracle`)
    — there is no hardcoded language-name comparison in this dispatch.

    For a ``kind="adapter"`` contract (Python) the legacy ``LayoutProfile`` is handed
    to the adapter as the layout VIEW (``layout_override``): the Python adapter reads
    ``source_root`` / ``test_root`` / ``package_root`` / ``package_name`` from it
    (the profile's ``LayoutSpec`` does not carry those). Go's command-sequence path
    needs no override (its adapter reads ``module_root`` from the ``LayoutSpec``).
    """
    if spec.kind == "composite":
        contract = _resolve_contract_oracle(profile.language)
        if contract is not None:
            lang_profile, oracle_decl, adapter = contract
            # A kind="adapter" oracle (Python) runs in-process and needs the richer
            # legacy layout (source_root/test_root/package_root); hand it the gate's
            # LayoutProfile as the layout view. A command-sequence oracle (Go) reads
            # only module_root from the profile's LayoutSpec → no override.
            layout_override = profile if getattr(oracle_decl, "kind", None) == "adapter" else None
            return _run_contract_oracle(
                project_root, lang_profile, oracle_decl, adapter, config,
                layout_override=layout_override,
            )
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

    CONTRACT-PATH languages (the language→oracle map entry): a language with no
    legacy ``LayoutProfile`` builder (Go — ``resolve_layout_profile('go')`` returns
    None) but a modeled ``implement_oracle`` whose adapter is REGISTERED resolves
    via :func:`_resolve_registry_composite_oracle`, which synthesizes a minimal
    ``(LayoutProfile, ImplementOracleSpec)`` (``kind="composite"``) so the existing
    gate machinery (certify/run/retry) works unchanged; the SAME dispatch in
    :func:`_run_oracle_command` then routes it to the Contract-Kernel contract path
    (:func:`_run_contract_oracle` → ``run_command_sequence`` + the registered
    adapter). The selection is GENERIC (modeled oracle + registered adapter) — no
    ``if language=='go'`` literal in the gate.
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


def _resolve_registry_composite_oracle(
    language: str | None,
) -> tuple[LayoutProfile, ImplementOracleSpec] | None:
    """Synthesize a composite ``(LayoutProfile, spec)`` from the registry, or None.

    De-literalized (Contract Kernel oracle dispatch §5): the old hardcoded
    ``{"go", "golang"}`` allowlist is gone. A language resolves to a synthesized
    composite IFF :func:`_resolve_contract_oracle` returns a triple — i.e. its
    resolved ``LanguageProfile`` declares a modeled COMPOSITE ``implement_oracle``
    whose adapter is REGISTERED (today only Go via ``go-toolchain``; Python/TS
    profiles resolve but their adapters are not yet registered → ``None`` → they
    stay on their legacy paths). For such a language with NO legacy
    ``LayoutProfile`` builder (Go — ``package_root.kind == none``), build a MINIMAL
    ``LayoutProfile`` carrying just ``language`` + the module root (as
    ``source_root``) so the existing gate machinery (run/certify/retry) works
    unchanged; the dispatch then routes its ``kind="composite"`` spec to the
    contract path (:func:`_run_contract_oracle`). ``None`` for any other language
    (a stack with neither a legacy profile nor a contract oracle stays a strict
    NO-OP). Best-effort: a registry error degrades to NO-OP (never a crash).
    """
    contract = _resolve_contract_oracle(language)
    if contract is None:
        return None
    lang_profile, oracle_decl, _adapter = contract
    # Only a COMPOSITE oracle is synthesized into a LayoutProfile here (the legacy
    # gate-machinery spec is composite-shaped); a command/adapter-kind contract
    # oracle would route differently (none today).
    if oracle_decl.kind != "composite":
        return None
    module_root = _norm(getattr(lang_profile.layout, "module_root", ".") or ".") or "."
    synthetic = LayoutProfile(
        language=lang_profile.id,  # canonical id ("go") — carried for the contract re-resolve
        package_name=lang_profile.id,
        source_root=module_root,  # carries the module root for the go commands' cwd
        package_root=module_root,
        test_root=module_root,
        implement_oracle=ImplementOracleSpec(
            command=f"{lang_profile.id}-composite",  # sentinel; kind dispatch runs the contract path
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
