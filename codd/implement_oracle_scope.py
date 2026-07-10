"""Scoped rerun derivation for the implement-time native oracle.

WHAT
====
When the implement-time oracle (``codd/implement_oracle.py``; TS = ``tsc
--noEmit``) REJECTS a build, the previous behaviour re-ran EVERY implement task
under the normalized feedback (a *broad* rerun). For five type errors in a couple
of files that regenerates the whole project (~17 units, ~40-50 min/attempt) —
unaffordable against the wall-clock.

This module localizes the rerun to the artifacts the diagnostics actually
implicate, WITHOUT losing cross-file-incoherence resilience. The pipeline
``rerun(feedback, scope)`` callback re-implements ONLY the scoped tasks; ``scope
is None`` means "broad" (the escalation fallback). The whole flow is:

    diagnostics → diagnostic edges → artifact paths → owning tasks → bounded scope

WHY NOT "re-implement the file tsc reported" (naive targeted)
============================================================
A type/symbol error is reported on the CONSUMER/importer, but the fix may belong
to the importer OR the exporter (GPT-5.5 Pro consult, 2026-06-14). ``src/index.ts``
importing ``runCli`` from ``./cli`` (which exports only ``run``) raises TS2305 on
``index.ts`` — yet the correct repair could be in ``cli.ts`` (add the export) or
``index.ts`` (import the right name). So the scope must include BOTH ENDS of the
broken demand edge. Naive targeted (importer only) leaves the exporter — the real
culprit half the time — outside the rerun.

WHY NOT the whole import-graph connected component
==================================================
A barrel ``src/index.ts`` re-exporting everything would balloon the scope back to
the whole project. The base scope is "both ends of the broken demand edge"; we
follow re-export chains only a bounded depth (``_REEXPORT_FOLLOW_DEPTH``).

ESCALATION LADDER (broad is DEMOTED, not removed)
=================================================
The fallback order is: narrow edge scope → expanded one-hop scope → broad → fail
honestly. We escalate when:
  * the diagnostics carry no determinable owner (no path / env-config), OR
  * the SAME diagnostic SIGNATURE survives a scoped rerun (the scope was too
    small — the real culprit was outside it), OR
  * the target set is too large (``> max(5 tasks, 30%)``) — at that breadth the
    cost gap to broad is small, so just go broad, OR
  * a public-API / shared-schema / entrypoint artifact with wide fan-out changed.

The signature is the loop-breaker: ``sorted((code, primary_path, symbol_or_module,
related_path))``. Same signature twice → expand; same after expand → broad; same
after broad → the caller raises a StageError. This guarantees termination.

TWO-LAYER DIAGNOSTIC PARSER (pragmatic today, extensible)
=========================================================
LAYER 1 (ideal) = the TypeScript compiler API / language service, which hands
back the COUNTERPART file of each diagnostic directly (the edge, for free).
LAYER 2 (implemented) = a regex parser over the tsc text PLUS edge derivation by
reading the importer file's own ``import``/``export`` declarations and resolving
the specifier through tsconfig (``baseUrl`` / ``paths`` / relative + the
NodeNext ``.js→.ts`` swap + ``index.*``). This recovers the exporter end without
spawning a Node process. ``_StructuredDiagnosticSource`` is the extension point
for layer 1: if/when a structured source is wired, ``derive_oracle_rerun_scope``
consumes its edges in preference to the regex layer. See the ``# TODO(layer-1)``.

This module is PURE and stack-parameterized: it never runs a compiler, never
edits files, and the only TS-specific knowledge (diagnostic code → edge class,
specifier resolution) lives behind small helpers a Go/Rust adapter can mirror.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol


__all__ = [
    "BroadRepairPlan",
    "DiagnosticEdge",
    "OracleRepairPhase",
    "OracleRerunScope",
    "OrphanArtifact",
    "OwnerUniquenessError",
    "OwnerUniquenessViolation",
    "ScopeDecision",
    "ScopeEscalation",
    "StructuredDiagnostic",
    "StructuredDiagnosticSource",
    "TaskOutputIndex",
    "find_orphan_artifacts",
    "build_path_owner_index",
    "classify_signature_progress",
    "derive_oracle_rerun_scope",
    "diagnostic_signature",
    "derive_residual_importer_scope",
    "exporter_surface_for_diagnostics",
    "extract_public_surface",
    "importers_of",
    "symbol_owners_for_diagnostics",
    "task_dependency_order",
    "validate_task_output_ownership_uniqueness",
]


# ── escalation ladder rungs (order matters; index = breadth) ──────────────────
SCOPE_NARROW = "narrow"  # both ends of the broken demand edge(s)
SCOPE_EXPANDED = "expanded"  # narrow + one import-hop neighbours of the edge ends
SCOPE_BROAD = "broad"  # every implement task (the legacy behaviour, now a fallback)
SCOPE_LADDER: tuple[str, ...] = (SCOPE_NARROW, SCOPE_EXPANDED, SCOPE_BROAD)


# ── broad-campaign phases (the incremental broad execution; GPT design §6) ────
#: ``broad`` is no longer "regenerate every task". When a wide-fan-out artifact
#: forces the broad RUNG, its EXECUTION is a budgeted residual-coherence campaign:
#: fix the shared supplier/exporter first, re-measure the whole-project oracle,
#: then fix ONLY the residual importers it still proves broken — never the whole
#: project. These name the campaign sub-phases (see :class:`OracleRepairPhase`).
PHASE_SUPPLIER_FIRST = "supplier_first"  # repair the shared exporter task(s) only
PHASE_RESIDUAL_IMPORTERS = "residual_importers"  # repair the still-broken importer owners
PHASE_CHUNKED_BROAD = "chunked_broad"  # last resort: all tasks, dependency-ordered, 1 pass
BROAD_CAMPAIGN_PHASES: tuple[str, ...] = (
    PHASE_SUPPLIER_FIRST,
    PHASE_RESIDUAL_IMPORTERS,
    PHASE_CHUNKED_BROAD,
)


def next_rung(rung: str) -> str | None:
    """The next-broader rung, or ``None`` past broad (→ caller fails honestly)."""
    try:
        idx = SCOPE_LADDER.index(rung)
    except ValueError:
        return SCOPE_BROAD
    return SCOPE_LADDER[idx + 1] if idx + 1 < len(SCOPE_LADDER) else None


# ── thresholds (the design's "too wide → just go broad" guards) ───────────────
#: A scope wider than ``max(_MAX_SCOPE_TASKS, _MAX_SCOPE_FRACTION × total)`` is
#: demoted to broad: at that breadth the locality win is gone and the cost gap to
#: a full rerun is small. Both bounds come straight from the GPT consult.
_MAX_SCOPE_TASKS = 5
_MAX_SCOPE_FRACTION = 0.30

#: How far to follow a re-export chain from an edge end (barrel ``index.ts`` →
#: real module). Bounded so a barrel does not pull the whole component.
_REEXPORT_FOLLOW_DEPTH = 2


# ── diagnostic-code → edge class (TS today; the only TS-specific table) ────────
#: "missing exported member" family — the error is on the IMPORTER; the
#: counterpart is the EXPORTER resolved from the importer's import declaration.
_TS_MISSING_EXPORT = frozenset({"TS2305", "TS2724", "TS2459", "TS2614"})
#: "cannot find module" family — the error is on the IMPORTER; the counterpart is
#: the set of candidate module paths for the unresolved specifier.
_TS_CANNOT_FIND_MODULE = frozenset({"TS2307", "TS2792", "TS6053", "TS5083"})
#: "cannot find name" family — primary file only, UNLESS the name is import-
#: derived (then promote to an importer→exporter edge).
_TS_CANNOT_FIND_NAME = frozenset({"TS2304", "TS2552"})
#: "property/type mismatch" family — primary file + the type-definition source
#: (relatedInformation if structured; else a 1-hop type-def import).
_TS_TYPE_MISMATCH = frozenset({"TS2339", "TS2322"})

#: A tsc diagnostic line (pretty ``path(line,col):`` or ``--pretty false``
#: ``path:line:col -``) → code + message + primary file. Mirrors the oracle's own
#: ``_TS_DIAG_LINE``/``_diag_path`` but keeps the per-line (code, path) pairing.
_TS_DIAG_PAREN = re.compile(
    r"^\s*(?P<path>[^\s(][^(\n]*\.(?:ts|tsx|mts|cts))\((?P<line>\d+),(?P<col>\d+)\):"
    r"\s*error\s+(?P<code>TS\d+)\s*:\s*(?P<message>.+?)\s*$",
    re.MULTILINE,
)
_TS_DIAG_COLON = re.compile(
    r"^\s*(?P<path>[^\s:][^:\n]*\.(?:ts|tsx|mts|cts)):(?P<line>\d+):(?P<col>\d+)"
    r"\s*-?\s*error\s+(?P<code>TS\d+)\s*:\s*(?P<message>.+?)\s*$",
    re.MULTILINE,
)

#: The module specifier inside a "no exported member" / "cannot find module"
#: message: ``Module '"./cli"' has no exported member 'runCli'`` /
#: ``Cannot find module './missing' or its ...``. Single OR double quoted.
_MSG_MODULE = re.compile(r"""(?:Module|module)\s+['"](?P<mod>[^'"]+)['"]|Cannot find module\s+['"](?P<mod2>[^'"]+)['"]""")
#: The demanded symbol in a "no exported member 'Y'" / "Cannot find name 'Y'".
_MSG_SYMBOL = re.compile(
    r"""(?:exported member(?:\s+named)?|Cannot find name|Property)\s+['"](?P<sym>[^'"]+)['"]"""
)

#: Import/export-with-specifier declarations in a TS source file, e.g.
#: ``import { run } from "./cli.js"`` / ``export { x } from "./mod"`` /
#: ``import type Foo from './foo'``. Captures the specifier so the exporter end
#: of an edge can be resolved without the compiler API.
_TS_IMPORT_FROM = re.compile(r"""\bfrom\s+['"](?P<spec>[^'"]+)['"]""")
#: Named bindings inside an import clause: ``import { a, b as c } from ...`` →
#: a, c. Used to decide whether a "cannot find name" is import-derived.
_TS_IMPORT_NAMES = re.compile(
    r"""\bimport\b[^;{]*\{(?P<names>[^}]*)\}\s*from\s*['"](?P<spec>[^'"]+)['"]""",
    re.MULTILINE,
)
_TS_IMPORT_DEFAULT = re.compile(
    r"""\bimport\s+(?:type\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*(?:,\s*\{[^}]*\}\s*)?from\s*['"](?P<spec>[^'"]+)['"]""",
)

# ── public-surface extraction (the "current export list" for contract feedback) ──
#: ``export { a, b as c }`` (a named re-export clause) — captures the brace body so
#: each exported (possibly aliased) name can be pulled. Covers ``export { x }`` and
#: ``export { x } from "./y"`` (the EXPORTED name is the alias, after ``as``).
_TS_EXPORT_NAMED_CLAUSE = re.compile(r"""\bexport\s+(?:type\s+)?\{(?P<names>[^}]*)\}""")
#: ``export const X`` / ``export function X`` / ``export class X`` /
#: ``export (abstract) class`` / ``export async function`` / ``export let|var`` /
#: ``export interface|type|enum X`` — the DECLARED public symbol.
_TS_EXPORT_DECL = re.compile(
    r"""\bexport\s+(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"""
    r"""(?:const|let|var|function\*?|class|interface|type|enum|namespace)\s+"""
    r"""(?P<name>[A-Za-z_$][\w$]*)""",
)
#: ``export default …`` — the default export (surfaced as the synthetic name
#: ``default`` so the SUT sees the module HAS a default vs only named exports).
_TS_EXPORT_DEFAULT = re.compile(r"""\bexport\s+default\b""")
#: ``export * from "./y"`` — a star re-export; we cannot enumerate the names
#: without following it, so we surface it verbatim as ``* from "./y"`` so the SUT
#: knows the surface is wider than the literal names listed.
_TS_EXPORT_STAR = re.compile(r"""\bexport\s+\*(?:\s+as\s+(?P<ns>[A-Za-z_$][\w$]*))?\s+from\s+['"](?P<spec>[^'"]+)['"]""")

#: Extension/candidate order for resolving a TS specifier to a real file. The
#: NodeNext ``.js`` specifier maps to a ``.ts`` source, so we swap extensions and
#: also try the bare path + each ext and ``index.*`` (a directory import).
_TS_SOURCE_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")

#: A diagnostic-implicated file with AT LEAST this many in-project importers is
#: treated as a wide-fan-out artifact (a public entrypoint / barrel / shared
#: schema). The design's broad trigger is "public-API/shared-schema/entrypoint
#: change WITH WIDE FAN-OUT" — the load-bearing qualifier is *measured* fan-out,
#: NOT a filename: a tiny ``index.ts`` that imports one module must stay scopable,
#: while a barrel re-exported by a dozen files goes broad. We MEASURE the fan-out
#: (count of project files importing the file) rather than match a basename, so
#: the guard is general (a Go/Rust port mirrors it) and never name-overfit.
_WIDE_FANOUT_IMPORTER_THRESHOLD = 6


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StructuredDiagnostic:
    """One diagnostic WITH its counterpart already resolved (layer-1 source).

    ``primary_path`` is the file tsc reported; ``related_path`` is the
    counterpart (the exporter for a missing-member error, the type-definition
    file for a property error) when the structured source can supply it. A regex
    fallback diagnostic has ``related_path=None`` and the edge is recovered by
    reading ``primary_path``'s imports (see ``_resolve_edge_from_source``).
    """

    code: str
    primary_path: str | None
    symbol: str | None = None
    module_specifier: str | None = None
    related_path: str | None = None
    message: str = ""


class StructuredDiagnosticSource(Protocol):
    """Layer-1 extension point: a structured diagnostics provider.

    An implementation (e.g. one that shells out to the TypeScript compiler API /
    language service, or parses ``tsc --generateTrace`` / a TS server protocol
    response) returns diagnostics with the COUNTERPART file already attached —
    which is exactly the edge the scope needs, for free. ``derive_oracle_rerun_scope``
    prefers a source's output over the regex layer when one is supplied.

    # TODO(layer-1): wire a concrete TypeScript-compiler-API source. The regex
    # layer below already recovers the exporter by reading the importer's imports,
    # so this is an accuracy upgrade (relatedInformation for TS2339/TS2322), not a
    # correctness prerequisite for the scoped-vs-broad ladder.
    """

    def diagnostics(self, output: str, project_root: Path) -> list[StructuredDiagnostic]:
        ...


@dataclass(frozen=True)
class DiagnosticEdge:
    """A broken demand edge: the importer end + every counterpart candidate.

    ``importer`` is the file the diagnostic was reported on; ``counterparts`` are
    the artifact paths the fix might instead belong to (the exporter, the
    candidate module paths, or the type-definition file). ``paths`` is the full
    artifact set the edge contributes to the scope = importer + counterparts.
    """

    code: str
    importer: str | None
    counterparts: tuple[str, ...] = ()
    symbol: str | None = None
    module_specifier: str | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.importer:
            out.append(self.importer)
        out.extend(self.counterparts)
        return tuple(dict.fromkeys(out))  # order-stable de-dupe


@dataclass(frozen=True)
class TaskOutputIndex:
    """A path → owning-task index (the design's ``path -> owner task`` map).

    Built from each task's declared ``output_paths`` UNION the first implement's
    actually-generated files UNION the config-derived output paths. ``owner_for``
    resolves a diagnostic path to its owning task id by exact match first, then
    by the nearest ancestor output DIRECTORY (a task that owns ``src/`` owns
    ``src/cli.ts`` even if it never declared that exact file).
    """

    #: exact relative-path → task_id
    exact: Mapping[str, str]
    #: output directory (relative, no trailing slash) → task_id, longest-first
    dirs: Sequence[tuple[str, str]]
    #: every known task id, in declaration order (for the broad fallback + frac)
    all_task_ids: tuple[str, ...]
    #: task_id → its planner task-level ``dependencies`` (the production graph).
    #: FIX-1 (Fable5 ts-v9 ruling): :func:`task_dependency_order` ranks by the
    #: longest-chain over THESE edges so a repair rerun regenerates producers
    #: before consumers. Empty ⇒ the shipped declaration-order fallback.
    dependencies: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def owner_for(self, path: str) -> str | None:
        norm = _norm(path)
        if not norm:
            return None
        hit = self.exact.get(norm)
        if hit is not None:
            return hit
        # Nearest ancestor directory wins (longest prefix). ``dirs`` is sorted
        # longest-first so the most specific owner is matched.
        for directory, task_id in self.dirs:
            if directory and (norm == directory or norm.startswith(directory + "/")):
                return task_id
        return None


@dataclass(frozen=True)
class OracleRerunScope:
    """The bounded set of tasks to re-implement (``None`` scope ⇒ broad).

    Carries the resolved ``task_ids`` AND the ``allowed_paths`` write-fence:
    re-implementing only the scoped tasks does NOT stop the SUT from writing
    OUT-of-scope files, so the caller restricts accepted writes to
    ``allowed_paths`` (the scoped tasks' output paths + the diagnostic-derived
    importer/exporter candidates + needed manifest/config) and reverts/hard-fails
    anything else. ``rung`` records which ladder level produced this scope (for
    logging + the escalation decision).
    """

    rung: str
    task_ids: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    #: diagnostic edges that produced the scope (diagnostics, for the SUT message)
    edges: tuple[DiagnosticEdge, ...] = ()
    #: human-readable why-this-scope detail (logged by the gate)
    detail: str = ""
    #: When the broad RUNG was forced by a wide-fan-out artifact, this carries the
    #: :class:`BroadRepairPlan` that turns broad's EXECUTION from "regenerate every
    #: task" into a budgeted residual-coherence campaign (supplier-first → residual
    #: importers → chunked broad). ``None`` for narrow/expanded scopes and for the
    #: legacy whole-project broad fallback. A scope whose ``repair_plan`` is set is
    #: a BROAD-CAMPAIGN scope: the gate branches to ``_execute_broad_campaign`` and
    #: the pipeline fences each phase to its own allowed paths.
    repair_plan: "BroadRepairPlan | None" = None

    def is_broad(self) -> bool:
        return self.rung == SCOPE_BROAD

    def is_broad_campaign(self) -> bool:
        """True when this is the incremental broad CAMPAIGN (carries a plan)."""
        return self.repair_plan is not None


@dataclass(frozen=True)
class ScopeDecision:
    """The outcome of one scope-derivation attempt.

    ``scope is None`` means "no determinable scoped target — escalate straight to
    broad" (e.g. diagnostics with no resolvable owner). Otherwise ``scope`` is a
    bounded :class:`OracleRerunScope`. ``force_broad`` is True when a narrow scope
    WAS derivable but a guard (too-wide / wide-fan-out artifact) demands broad
    anyway.
    """

    scope: OracleRerunScope | None
    force_broad: bool = False
    reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Broad-campaign plan (the incremental broad EXECUTION; GPT design §6)
# ─────────────────────────────────────────────────────────────────────────────
#
# A wide-fan-out artifact (src/validation.ts re-exported by a dozen files) forces
# the broad RUNG — but regenerating all ~17 tasks blows the wall-clock and a weak
# SUT loses coherence across them. The plan turns broad's EXECUTION into a
# budgeted residual-coherence campaign:
#
#   1. ``supplier_first``   — re-implement ONLY the supplier/exporter task(s) that
#      own the wide-fan-out focus artifact, fenced to {supplier outputs + focus
#      paths + manifest}. Often the type/export/schema fix on the supplier clears
#      every importer's diagnostic; if so the whole-project oracle passes and we
#      are done WITHOUT touching a single importer task.
#   2. ``residual_importers`` — re-derived from the diagnostics the whole-project
#      oracle STILL reports after the supplier fix: re-implement ONLY the owner
#      tasks of those residual importer paths, fenced to {their outputs + manifest}.
#   3. ``chunked_broad``    — last resort: all tasks in dependency order, one pass.
#
# The whole-project oracle is re-run after EVERY phase and is the ONLY green
# authority (a phase's local success proves nothing). The plan is bounded
# (supplier max-1/artifact; residual is a finite owner set; chunked-broad max-1
# pass) so the campaign always terminates. ``next_phase`` advances through the
# phases; the live residual-importer scope is re-derived by the gate from the
# CURRENT residual diagnostics (the plan's ``importer_task_ids`` is the upfront
# best-effort guess, refined per recheck), so the plan stays a declarative
# skeleton and the gate owns the per-recheck residual derivation.


@dataclass(frozen=True)
class OracleRepairPhase:
    """One sub-phase of a broad repair campaign (a scoped, fenced re-implement).

    ``phase`` is one of :data:`PHASE_SUPPLIER_FIRST` / :data:`PHASE_RESIDUAL_IMPORTERS`
    / :data:`PHASE_CHUNKED_BROAD`. ``scope`` is the :class:`OracleRerunScope` the
    pipeline re-implements for THIS phase (its ``task_ids`` + its ``allowed_paths``
    write-fence — so even a logically-broad campaign runs each phase fenced).
    ``focus_paths`` are the artifacts the phase centres on (the wide-fan-out
    supplier file for supplier_first; the residual importer paths for
    residual_importers). ``reason`` is the human-readable why-this-phase.
    """

    phase: str
    scope: OracleRerunScope
    focus_paths: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class BroadRepairPlan:
    """The plan a wide-fan-out broad RUNG executes instead of whole-project regen.

    ``focus_paths`` = the wide-fan-out artifact(s) that forced broad.
    ``supplier_task_ids`` = the task(s) that OWN those focus artifacts (the
    exporter end — repaired first). ``importer_task_ids`` = the owner tasks of the
    files that IMPORT the focus artifact(s) (the upfront best-effort residual
    candidate set; the gate refines it per recheck from the live residual
    diagnostics). ``phases`` = the ordered skeleton the campaign walks
    (supplier_first → residual_importers → chunked_broad), each a
    :class:`OracleRepairPhase` with its own fenced scope.

    Frozen + pure: built once at derivation time from the diagnostics + owner
    index; the gate consults it but never mutates it.
    """

    focus_paths: tuple[str, ...]
    supplier_task_ids: tuple[str, ...]
    importer_task_ids: tuple[str, ...]
    phases: tuple[OracleRepairPhase, ...] = ()

    def next_phase(self, completed: tuple[str, ...]) -> OracleRepairPhase | None:
        """The first skeleton phase whose name is not in ``completed``, else ``None``.

        Pure helper: the gate tracks which phase NAMES it has already executed and
        asks for the next one. ``None`` means the skeleton is exhausted (the
        campaign honest-fails if the whole-project oracle is still red).
        """
        done = set(completed)
        for phase in self.phases:
            if phase.phase not in done:
                return phase
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostic signature (the loop-breaker)
# ─────────────────────────────────────────────────────────────────────────────


def diagnostic_signature(diagnostics: Iterable[StructuredDiagnostic]) -> tuple[tuple[str, str, str, str], ...]:
    """A stable signature for a diagnostic SET (the infinite-loop guard).

    ``sorted((code, primary_path, symbol_or_module, related_path))`` per the
    design. Two oracle runs whose signatures are equal made no progress on the
    SAME incoherence, so the caller escalates the ladder. Paths are normalized;
    ``None`` becomes ``""`` so the tuple is always comparable/hashable.
    """
    sig: list[tuple[str, str, str, str]] = []
    for d in diagnostics:
        sym_or_mod = d.symbol or d.module_specifier or ""
        sig.append(
            (
                d.code or "",
                _norm(d.primary_path or ""),
                sym_or_mod,
                _norm(d.related_path or ""),
            )
        )
    return tuple(sorted(set(sig)))


# ─────────────────────────────────────────────────────────────────────────────
# Progress / oscillation classification (the escalation decision, set-based)
# ─────────────────────────────────────────────────────────────────────────────
#
# The previous loop-breaker escalated ONLY on ``signature == last_signature``
# (exact equality). That mis-reads OSCILLATION as progress: a non-deterministic
# SUT that, each scoped rerun, fixes some errors but INVENTS different new ones
# (20 → 4 → 6 diagnostics, all different sets) is never equal twice, so the gate
# stays at the same rung until the budget is spent — exactly the 2026-06-15
# codex11 failure. The fix (GPT-5.5 Pro consult, 2026-06-15) classifies the SET
# relation between consecutive signatures, not their equality:
#
#   strict_progress : curr ⊊ prev          → real shrink, same incoherence — STAY
#   soft_progress   : |curr| < |prev| AND  → fewer, but some NEW signatures; could
#                     few new signatures      be genuine progress OR slow drift —
#                                             allow ONCE per rung, then escalate
#   oscillation     : curr ⊄ prev AND       → not a shrink and not contained —
#                     |curr| >= |prev|        the SUT is thrashing — ESCALATE NOW
#   stuck           : curr == prev          → no movement at all — ESCALATE
#   (cycle)         : curr seen before in   → an A↔B↔A loop — ESCALATE (auxiliary,
#                     the bounded history     the relation tests above usually fire
#                                             first; cap is small so this is a net)
#
# All set-based: ``set(curr) < set(prev)`` is strict-subset; counts are the
# secondary signal (the GPT table's "count decrease is auxiliary, set is primary").
PROGRESS_STRICT = "strict_progress"
PROGRESS_SOFT = "soft_progress"
PROGRESS_OSCILLATION = "oscillation"
PROGRESS_STUCK = "stuck"
PROGRESS_CYCLE = "cycle"

#: A "soft progress" step may introduce at most this many NEW signature entries
#: and still count as (tentative) progress rather than oscillation. Small on
#: purpose: a shrink that swaps in MANY new errors is drift, not convergence.
_SOFT_PROGRESS_MAX_NEW = 2


def classify_signature_progress(
    current: tuple,
    previous: tuple | None,
    *,
    history: Sequence[tuple] = (),
) -> str:
    """Classify the SET relation between two diagnostic signatures.

    Returns one of ``PROGRESS_STRICT`` / ``PROGRESS_SOFT`` / ``PROGRESS_OSCILLATION``
    / ``PROGRESS_STUCK`` / ``PROGRESS_CYCLE``. ``previous is None`` (the first
    rerun, nothing to compare) is treated as ``PROGRESS_STRICT`` (give the current
    rung its turn). ``history`` is the bounded set of EARLIER signatures (excluding
    ``previous``); a ``current`` that reappears there is a cycle.

    The caller maps the result to an escalation decision: STRICT keeps the rung;
    SOFT keeps it AT MOST once per rung; OSCILLATION/STUCK/CYCLE escalate.
    """
    cur = frozenset(current)
    if previous is None:
        return PROGRESS_STRICT
    prev = frozenset(previous)
    if cur == prev:
        return PROGRESS_STUCK
    if cur and cur < prev:
        return PROGRESS_STRICT
    # A cycle: this exact signature was seen earlier (an A↔B↔A loop). Auxiliary —
    # checked before the soft/oscillation split so a smaller-but-recurring set is
    # not mistaken for soft progress.
    if any(cur == frozenset(h) for h in history):
        return PROGRESS_CYCLE
    new_signatures = cur - prev
    if len(cur) < len(prev) and len(new_signatures) <= _SOFT_PROGRESS_MAX_NEW:
        return PROGRESS_SOFT
    return PROGRESS_OSCILLATION


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: regex diagnostics → structured diagnostics
# ─────────────────────────────────────────────────────────────────────────────


def _parse_ts_diagnostics(output: str, project_root: Path) -> list[StructuredDiagnostic]:
    """Regex layer: tsc text → :class:`StructuredDiagnostic` (no counterpart yet).

    The counterpart (``related_path``) is left ``None`` here; it is resolved in
    :func:`_resolve_edge_from_source` by reading the importer's imports. This is
    the layer-2 fallback the module docstring describes.
    """
    text = output or ""
    out: list[StructuredDiagnostic] = []
    seen: set[tuple[str, str, int, int]] = set()
    for rx in (_TS_DIAG_PAREN, _TS_DIAG_COLON):
        for m in rx.finditer(text):
            code = m.group("code")
            primary = _project_relative(m.group("path"), project_root)
            key = (code, primary or "", int(m.group("line")), int(m.group("col")))
            if key in seen:
                continue
            seen.add(key)
            message = m.group("message").strip()
            mod = _extract_module_specifier(message)
            sym = _extract_symbol(message)
            out.append(
                StructuredDiagnostic(
                    code=code,
                    primary_path=primary,
                    symbol=sym,
                    module_specifier=mod,
                    message=message,
                )
            )
    return out


def _extract_module_specifier(message: str) -> str | None:
    m = _MSG_MODULE.search(message)
    if m is None:
        return None
    return m.group("mod") or m.group("mod2")


def _extract_symbol(message: str) -> str | None:
    m = _MSG_SYMBOL.search(message)
    return m.group("sym") if m else None


# ─────────────────────────────────────────────────────────────────────────────
# diagnostics → edges
# ─────────────────────────────────────────────────────────────────────────────


def _edges_from_diagnostics(
    diagnostics: Sequence[StructuredDiagnostic], project_root: Path
) -> list[DiagnosticEdge]:
    """Build the broken-demand EDGE for each diagnostic (per the code-class table)."""
    edges: list[DiagnosticEdge] = []
    for d in diagnostics:
        edge = _edge_for_diagnostic(d, project_root)
        if edge is not None:
            edges.append(edge)
    return edges


def _edge_for_diagnostic(d: StructuredDiagnostic, project_root: Path) -> DiagnosticEdge | None:
    code = d.code
    importer = d.primary_path
    # A structured source may already carry the counterpart — prefer it.
    structured_counterpart = (d.related_path,) if d.related_path else ()

    if code in _TS_MISSING_EXPORT:
        # importer + the exporter resolved from the importer's import of the
        # message's module specifier (or the importer's import of the symbol).
        counterparts = list(structured_counterpart)
        counterparts += _resolve_edge_from_source(d, project_root)
        return DiagnosticEdge(
            code=code,
            importer=importer,
            counterparts=tuple(dict.fromkeys(counterparts)),
            symbol=d.symbol,
            module_specifier=d.module_specifier,
        )

    if code in _TS_CANNOT_FIND_MODULE:
        # importer + candidate module paths for the unresolved specifier.
        counterparts = list(structured_counterpart)
        counterparts += _candidate_module_paths(d, project_root)
        return DiagnosticEdge(
            code=code,
            importer=importer,
            counterparts=tuple(dict.fromkeys(counterparts)),
            symbol=d.symbol,
            module_specifier=d.module_specifier,
        )

    if code in _TS_CANNOT_FIND_NAME:
        # primary file only — UNLESS the name is import-derived, then promote to
        # an importer→exporter edge.
        counterparts = list(structured_counterpart)
        if d.symbol and importer:
            counterparts += _exporter_for_imported_name(importer, d.symbol, project_root)
        return DiagnosticEdge(
            code=code,
            importer=importer,
            counterparts=tuple(dict.fromkeys(counterparts)),
            symbol=d.symbol,
        )

    if code in _TS_TYPE_MISMATCH:
        # primary + the type-definition source: relatedInformation if structured,
        # else a 1-hop type-def import from the primary file.
        counterparts = list(structured_counterpart)
        if not counterparts and importer:
            counterparts += _type_def_imports(importer, project_root)
        return DiagnosticEdge(
            code=code,
            importer=importer,
            counterparts=tuple(dict.fromkeys(counterparts)),
            symbol=d.symbol,
        )

    # Any other code: the primary file is the edge (a single-ended edge). Still
    # better than broad, and the signature guard will escalate if it is wrong.
    if importer:
        return DiagnosticEdge(code=code, importer=importer, counterparts=structured_counterpart)
    return None


def _resolve_edge_from_source(d: StructuredDiagnostic, project_root: Path) -> list[str]:
    """Resolve the EXPORTER end of a missing-export edge by reading the importer.

    Reads the importer file, finds the import whose specifier matches the
    diagnostic's module specifier (or, failing that, the import that binds the
    diagnostic's symbol), and resolves that specifier to a real project file —
    following a re-export chain a bounded depth (barrel ``index.ts`` →
    real module). This is the layer-2 recovery of the counterpart the TypeScript
    compiler API would hand back directly.
    """
    if not d.primary_path:
        return []
    importer_abs = (project_root / d.primary_path).resolve()
    content = _read(importer_abs)
    if content is None:
        return []
    specs = _matching_specifiers(content, module_specifier=d.module_specifier, symbol=d.symbol)
    resolved: list[str] = []
    for spec in specs:
        target = _resolve_specifier(importer_abs.parent, spec, project_root)
        if target is not None:
            resolved.append(target)
            resolved.extend(
                _follow_reexports(target, d.symbol, project_root, depth=_REEXPORT_FOLLOW_DEPTH)
            )
    return list(dict.fromkeys(resolved))


def _matching_specifiers(content: str, *, module_specifier: str | None, symbol: str | None) -> list[str]:
    """Specifiers in ``content`` that match the diagnostic's module or symbol."""
    specs: list[str] = []
    # 1. The message named a module specifier → prefer the import of THAT module.
    if module_specifier:
        for m in _TS_IMPORT_FROM.finditer(content):
            if _specifier_key(m.group("spec")) == _specifier_key(module_specifier):
                specs.append(m.group("spec"))
    # 2. Else (or also) the import that binds the missing symbol.
    if symbol:
        for m in _TS_IMPORT_NAMES.finditer(content):
            names = [n.strip().split(" as ")[-1].strip() for n in m.group("names").split(",")]
            names = [n for n in names if n]
            if symbol in names or any(part.split(" as ")[0].strip() == symbol for part in m.group("names").split(",")):
                specs.append(m.group("spec"))
        for m in _TS_IMPORT_DEFAULT.finditer(content):
            if m.group("name") == symbol:
                specs.append(m.group("spec"))
    return list(dict.fromkeys(specs))


def _specifier_key(spec: str) -> str:
    """Normalize a specifier for matching (drop a trailing ``.js``/``.ts`` ext)."""
    s = spec.strip()
    for ext in _TS_SOURCE_EXTS:
        if s.endswith(ext):
            return s[: -len(ext)]
    return s


def _exporter_for_imported_name(importer: str, symbol: str, project_root: Path) -> list[str]:
    """If ``symbol`` is import-derived in ``importer``, resolve its exporter."""
    content = _read((project_root / importer).resolve())
    if content is None:
        return []
    return _resolve_edge_from_source(
        StructuredDiagnostic(code="", primary_path=importer, symbol=symbol), project_root
    )


def _type_def_imports(importer: str, project_root: Path) -> list[str]:
    """1-hop: every project file the ``importer`` imports (type-def candidates).

    For a TS2339/TS2322 with no structured relatedInformation we cannot know
    WHICH import declares the offending type, so we add the importer's relative
    imports (bounded — the importer's own first-hop). This stays well short of
    the connected component (no transitive follow) and the signature guard
    escalates if the real type-def was one hop further.
    """
    content = _read((project_root / importer).resolve())
    if content is None:
        return []
    importer_abs = (project_root / importer).resolve()
    resolved: list[str] = []
    for m in _TS_IMPORT_FROM.finditer(content):
        spec = m.group("spec")
        if not spec.startswith("."):
            continue
        target = _resolve_specifier(importer_abs.parent, spec, project_root)
        if target is not None:
            resolved.append(target)
    return list(dict.fromkeys(resolved))


def _candidate_module_paths(d: StructuredDiagnostic, project_root: Path) -> list[str]:
    """Candidate file paths for an unresolved module specifier (TS2307/2792).

    The module does not exist yet, so we cannot resolve it to a real file; we
    emit the relative candidate paths (``foo.ts``, ``foo.tsx``, ``foo/index.ts``,
    …) so whichever task OWNS that location is pulled into scope (it failed to
    create the module). Bare/package specifiers yield nothing (an environment/
    dependency issue, not a code-owner one).
    """
    spec = d.module_specifier
    if not spec or not spec.startswith(".") or not d.primary_path:
        return []
    importer_abs = (project_root / d.primary_path).resolve()
    base = (importer_abs.parent / spec).resolve()
    out: list[str] = []
    stem = base.with_suffix("") if base.suffix else base
    for ext in _TS_SOURCE_EXTS:
        out.append(_to_relative(stem.with_suffix(ext), project_root))
        out.append(_to_relative(base / f"index{ext}", project_root))
    return [p for p in dict.fromkeys(out) if p]


def _follow_reexports(target_rel: str, symbol: str | None, project_root: Path, *, depth: int) -> list[str]:
    """Follow ``export { X } from "./real"`` re-export chains, bounded by ``depth``.

    A barrel module that re-exports the symbol points at the REAL owner; we add
    that owner (and continue a bounded depth) WITHOUT pulling the barrel's whole
    surface. Only re-exports that plausibly carry ``symbol`` (or ``*``) are
    followed when a symbol is known.
    """
    if depth <= 0:
        return []
    content = _read((project_root / target_rel).resolve())
    if content is None:
        return []
    target_abs = (project_root / target_rel).resolve()
    out: list[str] = []
    for m in re.finditer(
        r"""\bexport\b(?P<clause>[^;]*?)\bfrom\s*['"](?P<spec>[^'"]+)['"]""", content
    ):
        clause = m.group("clause")
        spec = m.group("spec")
        if not spec.startswith("."):
            continue
        if symbol and "*" not in clause and "{" in clause:
            names = re.findall(r"[A-Za-z_$][\w$]*", clause)
            if symbol not in names:
                continue
        nxt = _resolve_specifier(target_abs.parent, spec, project_root)
        if nxt and nxt != target_rel:
            out.append(nxt)
            out.extend(_follow_reexports(nxt, symbol, project_root, depth=depth - 1))
    return list(dict.fromkeys(out))


# ─────────────────────────────────────────────────────────────────────────────
# Public-surface extraction (the EXPORTER's current interface, for contract feedback)
# ─────────────────────────────────────────────────────────────────────────────
#
# WHY: an oracle failure feedback that only echoes the IMPORTER's error
# ("./helpers has no exported member expectSuccess") makes the SUT GUESS what the
# exporter actually offers — and a non-deterministic SUT guesses a DIFFERENT wrong
# symbol each rerun (the 2026-06-15 codex11 oscillation: it invented
# ``expectSuccess`` then a different shape then dropped the vitest import). Showing
# the exporter's CURRENT public surface ("./helpers exports {runTempconv, projectRoot}")
# turns "invent a plausible name" into "reconcile to one of THESE". This is the
# load-bearing convergence lever (GPT-5.5 Pro consult, 2026-06-15: feedback 3a).
#
# LANGUAGE-AGNOSTIC: ``extract_public_surface`` dispatches on file extension to a
# per-language extractor. TS has one (regex-level, mirroring the import parser);
# an unknown language returns ``None`` (graceful degradation — the gate simply
# omits the surface line and keeps the generic guidance, never crashes). A Go/Rust
# port adds one extractor entry, never a core edit.


def extract_public_surface(path: str, project_root: Path) -> list[str] | None:
    """The current public export surface of ``path`` → a list of names, or ``None``.

    ``None`` means "no extractor for this file kind" (graceful degradation — the
    caller omits the surface from feedback rather than fabricating one). An empty
    list means "extractor ran, the file exports NOTHING" (a meaningful signal: the
    importer demands a symbol from a module with no exports at all). Names are the
    EXPORTED identifiers (the alias after ``as`` for a renamed re-export), plus the
    synthetic ``default`` for a default export and a verbatim ``* from "<spec>"``
    for an un-enumerable star re-export.
    """
    suffix = PurePosixPath(path).suffix
    if suffix in _TS_SOURCE_EXTS:
        content = _read((project_root / path).resolve())
        if content is None:
            return None
        return _ts_public_surface(content)
    return None


def _ts_public_surface(content: str) -> list[str]:
    """Extract the exported names from TypeScript/JavaScript source (regex level).

    Covers named-declaration exports (``export const/function/class/interface/
    type/enum X``), named-clause exports (``export { a, b as c }`` incl.
    ``export { x } from "./y"`` re-exports — the EXPORTED name is the alias),
    ``export default`` (→ the synthetic name ``default``), and ``export * from
    "./y"`` (→ ``* from "./y"``). Order-stable, de-duplicated.
    """
    names: list[str] = []
    for m in _TS_EXPORT_DECL.finditer(content):
        names.append(m.group("name"))
    for m in _TS_EXPORT_NAMED_CLAUSE.finditer(content):
        for raw in m.group("names").split(","):
            part = raw.strip()
            if not part:
                continue
            # ``a as b`` exports ``b``; ``a`` exports ``a``. Strip a type-only
            # keyword prefix (``type X``) that some clauses carry per-name.
            exported = part.split(" as ")[-1].strip()
            exported = exported.removeprefix("type ").strip()
            if exported:
                names.append(exported)
    if _TS_EXPORT_DEFAULT.search(content):
        names.append("default")
    for m in _TS_EXPORT_STAR.finditer(content):
        ns = m.group("ns")
        spec = m.group("spec")
        names.append(f"{ns} (* as) from \"{spec}\"" if ns else f"* from \"{spec}\"")
    return list(dict.fromkeys(names))


def exporter_surface_for_diagnostics(
    diagnostics: Sequence[StructuredDiagnostic],
    project_root: Path,
    *,
    structured_source: StructuredDiagnosticSource | None = None,
) -> dict[str, list[str]]:
    """Map each diagnostic's EXPORTER path → its current public surface.

    For every missing-export / cannot-find-module / import-derived-name
    diagnostic, resolve the exporter end of the broken edge (the same resolution
    the scope derivation uses — :func:`_edge_for_diagnostic`) and extract that
    file's current public surface. Returns ``{exporter_rel_path: [export names]}``;
    a path whose extractor returns ``None`` (unknown language) is omitted. This is
    the data the gate folds into the SUT-facing feedback so the SUT reconciles its
    imports to the REAL surface instead of re-guessing (and re-oscillating).

    Pure + best-effort: any per-diagnostic failure is skipped (a partial surface
    map is still useful; never abort feedback assembly).
    """
    del structured_source  # reserved: a layer-1 source could supply surfaces directly
    surfaces: dict[str, list[str]] = {}
    for d in diagnostics:
        try:
            edge = _edge_for_diagnostic(d, project_root)
        except Exception:  # noqa: BLE001 — one bad diagnostic must not kill the map.
            continue
        if edge is None:
            continue
        # The counterparts are the exporter candidate(s); the importer is the
        # error site (its own surface is rarely the fix target, so we skip it).
        for candidate in edge.counterparts:
            norm = _norm(candidate)
            if not norm or norm in surfaces:
                continue
            surface = extract_public_surface(norm, project_root)
            if surface is not None:
                surfaces[norm] = surface
    return surfaces


def symbol_owners_for_diagnostics(
    diagnostics: Sequence[StructuredDiagnostic],
    project_root: Path,
) -> dict[str, list[str]]:
    """Map each missing/unexported symbol → the file(s) that ACTUALLY export it.

    FIX-2 (Fable5 ts-v9 ruling). The exporter-surface block names what the
    DEMANDED module exports; it never says WHERE a missing symbol truly lives. So
    when the design steered a consumer to import ``ExprNode`` from ``parser`` but
    ``ast.ts`` is its real owner, the rerun had no evidence to rewrite ``./parser``
    → ``./ast`` and re-transcribed the defect. This scans the generated tree with
    the EXISTING name-level surface extractor (:func:`extract_public_surface`) and
    reports the real owner ("``ExprNode`` is exported by ``src/ast.ts``").

    The diagnostics' own implicated files (``primary_path``/``related_path`` — the
    broken importer and the wrong exporter) are EXCLUDED so the broken consumer is
    never named as the authority. Read-only, deterministic (files + owners sorted),
    and silently empty where no owner exists — a symbol exported NOWHERE stays a
    red (anti-false-green: this names real exporters from disk, it invents none).
    """
    wanted = _dedupe(str(d.symbol).strip() for d in diagnostics if d.symbol)
    if not wanted:
        return {}
    wanted_set = set(wanted)
    excluded = {
        _norm(p)
        for d in diagnostics
        for p in (d.primary_path, d.related_path)
        if p
    }
    owners: dict[str, list[str]] = {}
    for file_path in sorted(_iter_project_source_files(project_root), key=lambda p: str(p)):
        rel = _to_relative(file_path, project_root)
        if not rel or rel in excluded:
            continue
        try:
            surface = extract_public_surface(rel, project_root)
        except Exception:  # noqa: BLE001 — one unreadable file must not kill the map.
            continue
        if not surface:
            continue
        for name in surface:
            if name in wanted_set and rel not in owners.setdefault(name, []):
                owners[name].append(rel)
    # Preserve the diagnostics' symbol order; drop symbols with no real owner.
    return {sym: owners[sym] for sym in wanted if owners.get(sym)}


# ─────────────────────────────────────────────────────────────────────────────
# Specifier resolution (relative + tsconfig baseUrl/paths)
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_specifier(base_dir: Path, spec: str, project_root: Path) -> str | None:
    """Resolve a TS import specifier to a project-relative file, or ``None``.

    Handles relative (``./x``, ``../x``) specifiers directly and bare specifiers
    via tsconfig ``baseUrl``/``paths`` (best-effort). Tries the literal path, the
    NodeNext ``.js→.ts`` extension swap, the bare path + each extension, and
    ``index.*``. Never returns a path outside the project tree.
    """
    if spec.startswith("."):
        return _resolve_relative(base_dir, spec, project_root)
    return _resolve_via_tsconfig_paths(spec, project_root)


def _resolve_relative(base_dir: Path, spec: str, project_root: Path) -> str | None:
    raw = (base_dir / spec).resolve()
    for candidate in _candidates_for(raw):
        if candidate.is_file():
            return _to_relative(candidate, project_root) or None
    return None


def _candidates_for(raw: Path) -> list[Path]:
    candidates: list[Path] = []
    if raw.suffix:
        candidates.append(raw)
        stem = raw.with_suffix("")
        for ext in _TS_SOURCE_EXTS:
            candidates.append(stem.with_suffix(ext))
    else:
        for ext in _TS_SOURCE_EXTS:
            candidates.append(raw.with_suffix(ext))
    for ext in _TS_SOURCE_EXTS:
        candidates.append(raw / f"index{ext}")
    return candidates


def _resolve_via_tsconfig_paths(spec: str, project_root: Path) -> str | None:
    """Resolve a bare specifier through tsconfig ``baseUrl``/``paths`` (best-effort).

    Reads ``tsconfig.json`` (JSONC-tolerant via the oracle's stripper), applies a
    matching ``paths`` alias (``@app/*`` → ``src/*``) or ``baseUrl`` root, and
    resolves the rewritten path with the same candidate set. Missing/invalid
    tsconfig → ``None`` (the specifier is treated as a package import).
    """
    cfg = _load_tsconfig(project_root)
    if cfg is None:
        return None
    compiler = cfg.get("compilerOptions") if isinstance(cfg.get("compilerOptions"), dict) else {}
    base_url = compiler.get("baseUrl") if isinstance(compiler, dict) else None
    paths = compiler.get("paths") if isinstance(compiler, dict) else None
    base_root = project_root
    if isinstance(base_url, str) and base_url.strip():
        base_root = (project_root / base_url).resolve()

    # 1. paths alias.
    if isinstance(paths, dict):
        for pattern, targets in paths.items():
            rewritten = _apply_paths_alias(spec, pattern, targets)
            if rewritten is None:
                continue
            for rw in rewritten:
                resolved = _resolve_relative(base_root, "./" + rw if not rw.startswith(".") else rw, project_root)
                if resolved:
                    return resolved
    # 2. bare baseUrl-relative resolution.
    if isinstance(base_url, str) and base_url.strip():
        resolved = _resolve_relative(base_root, "./" + spec, project_root)
        if resolved:
            return resolved
    return None


def _apply_paths_alias(spec: str, pattern: str, targets: object) -> list[str] | None:
    """Apply a single tsconfig ``paths`` entry to ``spec`` → rewritten target(s)."""
    if not isinstance(targets, list):
        return None
    target_strs = [str(t) for t in targets if isinstance(t, str)]
    if not target_strs:
        return None
    if "*" in pattern:
        prefix, _, suffix = pattern.partition("*")
        if not spec.startswith(prefix) or not spec.endswith(suffix):
            return None
        middle = spec[len(prefix) : len(spec) - len(suffix) if suffix else None]
        return [t.replace("*", middle) for t in target_strs]
    if spec == pattern:
        return target_strs
    return None


_TSCONFIG_CACHE: dict[str, dict | None] = {}


def _load_tsconfig(project_root: Path) -> dict | None:
    key = str(project_root.resolve())
    if key in _TSCONFIG_CACHE:
        return _TSCONFIG_CACHE[key]
    import json

    from codd.implement_oracle import _strip_jsonc

    path = project_root / "tsconfig.json"
    result: dict | None = None
    try:
        if path.is_file():
            payload = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
            if isinstance(payload, dict):
                result = payload
    except (OSError, ValueError):
        result = None
    _TSCONFIG_CACHE[key] = result
    return result


# ─────────────────────────────────────────────────────────────────────────────
# path → owning task index
# ─────────────────────────────────────────────────────────────────────────────


def build_path_owner_index(
    tasks: Sequence[object],
    *,
    project_root: Path,
    config: Mapping[str, object] | None = None,
    generated_files: Mapping[str, Iterable[str]] | None = None,
    config_output_paths: Mapping[str, Iterable[str]] | None = None,
) -> TaskOutputIndex:
    """Build the path→owner-task index from declared + generated + config paths.

    Per the design, three sources are unioned per task:
      1. ``task.output_paths`` — the task's DECLARED outputs.
      2. ``generated_files[task_id]`` — what the FIRST implement actually wrote
         (caught files a task declared only as a directory).
      3. ``config_output_paths[task_id]`` — config-derived output paths
         (``_output_paths_for_task`` in the pipeline supplies these).

    A path is indexed both EXACTLY (file → owner) and by DIRECTORY (an output dir
    → owner) so a diagnostic on ``src/cli.ts`` resolves to the task that owns
    ``src/`` when the exact file was not declared. Directories are sorted
    longest-first so the most specific owner wins.
    """
    exact: dict[str, str] = {}
    dir_owner: dict[str, str] = {}
    all_ids: list[str] = []
    deps: dict[str, tuple[str, ...]] = {}
    gen = generated_files or {}
    cfg_paths = config_output_paths or {}

    for task in tasks:
        task_id = _task_id(task)
        if task_id is None:
            continue
        all_ids.append(task_id)
        deps.setdefault(task_id, _task_dependencies(task))
        declared = list(_task_output_paths(task) or ())
        declared += list(gen.get(task_id, ()) or ())
        declared += list(cfg_paths.get(task_id, ()) or ())
        for raw in declared:
            norm = _norm(raw)
            if not norm:
                continue
            if _looks_like_file(norm):
                exact.setdefault(norm, task_id)
                # The file's parent dir is also (weakly) owned by this task.
                parent = str(PurePosixPath(norm).parent)
                if parent and parent != ".":
                    dir_owner.setdefault(parent, task_id)
            else:
                dir_owner.setdefault(norm, task_id)

    dirs_sorted = sorted(dir_owner.items(), key=lambda kv: len(kv[0]), reverse=True)
    return TaskOutputIndex(
        exact=exact,
        dirs=tuple(dirs_sorted),
        all_task_ids=tuple(dict.fromkeys(all_ids)),
        dependencies=deps,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Output-owner uniqueness invariant (ACG: exactly ONE owner per artifact)
# ─────────────────────────────────────────────────────────────────────────────
#
# THE INVARIANT (GPT-5.5 Pro round-2 §3.3 — the "空セル" the orphan gate left):
#   ``build_path_owner_index`` registers exact paths and directory owners with
#   ``setdefault``, so when two tasks declare the SAME output the FIRST silently
#   wins. The orphan gate checks "at least one owner"; this checks the OTHER half
#   of ACG ownership — "at MOST one owner". A duplicate owner makes a scoped
#   rerun's responsibility/write-fence ambiguous (which task regenerates the
#   shared file? whose fence governs it?), so the same false-green class the
#   orphan gate closes re-opens from the other side.
#
# DETERMINISTIC + BEFORE implement-oracle: this is a PURE structural check over
# the SAME three path sources ``build_path_owner_index`` unions (declared output
# paths + first-implement generated files + config-derived paths). It runs at
# index-build time (the pipeline calls it just before ``build_path_owner_index``)
# so a duplicate-owner topology honest-fails up-front, not after a 40-minute
# rerun discovers the ambiguity. Language-agnostic: no per-language logic — it
# reasons purely about declared path strings.
#
# WHAT COUNTS AS A CONFLICT (the three cases GPT §3.3 names):
#   1. exact-vs-exact   — two tasks declare the SAME exact file path.
#   2. dir-vs-exact     — task A owns directory ``src/`` and task B declares the
#      exact file ``src/x.ts`` (B's file sits inside A's owned tree → two owners).
#   3. dir-vs-dir       — task A owns ``src/`` and task B owns ``src/lib/`` (B's
#      tree nests inside A's → overlapping directory ownership).
#
# A file's PARENT directory is owned only WEAKLY by the file's task (the same
# ``setdefault`` semantics ``build_path_owner_index`` uses), so a task owning
# ``src/a.ts`` and another owning ``src/b.ts`` do NOT conflict (the weak
# ``src`` ownership of one does not steal the other's file). We mirror that:
# weak parent-dir ownership never triggers a dir conflict; only an EXPLICITLY
# declared directory output does.


@dataclass(frozen=True)
class OwnerUniquenessViolation:
    """One artifact-ownership conflict (a path claimed by >1 task)."""

    #: ``"duplicate_exact"`` | ``"dir_file_conflict"`` | ``"overlapping_dirs"``
    kind: str
    path: str
    owners: tuple[str, ...]
    detail: str = ""

    @property
    def message(self) -> str:
        owners = ", ".join(self.owners)
        if self.kind == "duplicate_exact":
            return (
                f"output path {self.path!r} is declared by {len(self.owners)} tasks "
                f"({owners}) — exactly one task must own an artifact"
            )
        if self.kind == "dir_file_conflict":
            return (
                f"output {self.path!r} has both a directory owner and an exact-file "
                f"owner ({owners}) — its repair scope/write-fence is ambiguous"
            )
        return (
            f"output directories overlap ({self.detail}; owners {owners}) — nested "
            f"directory ownership makes the inner files doubly owned"
        )


class OwnerUniquenessError(ValueError):
    """Raised when ≥1 generated artifact would have more than one owning task.

    Carries the structured :attr:`violations` so the caller (the greenfield
    pipeline) can surface them in a single StageError. A ``ValueError`` subclass
    (not a pipeline import) so this module stays dependency-free of the pipeline;
    the pipeline catches it and re-raises as a ``StageError``.
    """

    def __init__(self, violations: "Sequence[OwnerUniquenessViolation]") -> None:
        self.violations = tuple(violations)
        joined = "; ".join(v.message for v in self.violations)
        super().__init__(
            f"output-owner uniqueness violated ({len(self.violations)} conflict(s)): {joined}"
        )


def validate_task_output_ownership_uniqueness(
    tasks: Sequence[object],
    *,
    generated_files: Mapping[str, Iterable[str]] | None = None,
    config_output_paths: Mapping[str, Iterable[str]] | None = None,
) -> None:
    """Raise :class:`OwnerUniquenessError` if any artifact would have >1 owner.

    PURE + deterministic. Reasons over the SAME three path sources
    :func:`build_path_owner_index` unions per task — declared ``output_paths``,
    ``generated_files[task_id]``, ``config_output_paths[task_id]`` — and detects
    the three GPT §3.3 conflict classes (exact-vs-exact, dir-vs-exact,
    dir-vs-dir). No conflict ⇒ returns ``None``. Intended to run at index-build
    time, BEFORE the implement-oracle, so an ambiguous-ownership topology fails
    fast and honestly rather than letting the first-owner-wins ``setdefault``
    silently pick a winner.

    Only EXPLICITLY declared directory outputs participate in directory-conflict
    detection (a file's parent dir is weakly owned and never steals another
    task's file — mirroring ``build_path_owner_index``'s ``setdefault`` parent
    handling). Duplicate self-declarations by the SAME task are not a conflict
    (a task may list a path twice / declare it AND generate it).
    """
    gen = generated_files or {}
    cfg_paths = config_output_paths or {}

    # exact file path -> set of owning task ids; declared directory -> owners.
    exact_owners: dict[str, list[str]] = {}
    dir_owners: dict[str, list[str]] = {}

    for task in tasks:
        task_id = _task_id(task)
        if task_id is None:
            continue
        declared = list(_task_output_paths(task) or ())
        declared += list(gen.get(task_id, ()) or ())
        declared += list(cfg_paths.get(task_id, ()) or ())
        # De-dupe within a task: the SAME path declared twice by one task is not a
        # multi-owner conflict.
        seen_files: set[str] = set()
        seen_dirs: set[str] = set()
        for raw in declared:
            norm = _norm(raw)
            if not norm:
                continue
            if _looks_like_file(norm):
                if norm in seen_files:
                    continue
                seen_files.add(norm)
                owners = exact_owners.setdefault(norm, [])
                if task_id not in owners:
                    owners.append(task_id)
            else:
                if norm in seen_dirs:
                    continue
                seen_dirs.add(norm)
                owners = dir_owners.setdefault(norm, [])
                if task_id not in owners:
                    owners.append(task_id)

    violations: list[OwnerUniquenessViolation] = []

    # 1. exact-vs-exact: a file path with more than one owning task.
    for path, owners in sorted(exact_owners.items()):
        if len(owners) > 1:
            violations.append(
                OwnerUniquenessViolation(
                    kind="duplicate_exact", path=path, owners=tuple(owners)
                )
            )

    # 2. dir-vs-exact: an explicitly-declared directory owner whose tree contains
    #    an exact-file owner that is a DIFFERENT task.
    for directory, dir_owner_ids in sorted(dir_owners.items()):
        for file_path, file_owner_ids in exact_owners.items():
            if file_path == directory or file_path.startswith(directory + "/"):
                conflicting = sorted(set(dir_owner_ids) | set(file_owner_ids))
                if len(conflicting) > 1:
                    violations.append(
                        OwnerUniquenessViolation(
                            kind="dir_file_conflict",
                            path=file_path,
                            owners=tuple(conflicting),
                            detail=f"file under directory owner {directory!r}",
                        )
                    )

    # 3. dir-vs-dir: two explicitly-declared directory owners that nest, owned by
    #    different tasks (src/ vs src/lib/). Compare each ordered pair once.
    dir_items = sorted(dir_owners.items())
    for i, (outer, outer_owners) in enumerate(dir_items):
        for inner, inner_owners in dir_items[i + 1 :]:
            # ``inner`` is the longer-or-equal path in sorted order? Not guaranteed;
            # test nesting both directions.
            a, b = outer, inner
            if a == b:
                continue
            nested = b.startswith(a + "/") or a.startswith(b + "/")
            if not nested:
                continue
            conflicting = sorted(set(outer_owners) | set(inner_owners))
            if len(conflicting) > 1:
                violations.append(
                    OwnerUniquenessViolation(
                        kind="overlapping_dirs",
                        path=f"{a} / {b}",
                        owners=tuple(conflicting),
                        detail=f"directory {a!r} nests {b!r}"
                        if b.startswith(a + "/")
                        else f"directory {b!r} nests {a!r}",
                    )
                )

    if violations:
        raise OwnerUniquenessError(violations)


# ─────────────────────────────────────────────────────────────────────────────
# Orphan artifact invariant (ACG: every generated artifact must have an owner)
# ─────────────────────────────────────────────────────────────────────────────
#
# THE INVARIANT (GPT-5.5 Pro consult, 2026-06-15 — a natural ACG implication):
#   1. Every generated artifact is owned by exactly one task (or an explicit
#      harness/profile contract).
#   2. Every public demand edge resolves to an owned supplier or is removed.
#   3. A scoped rerun may not create an unowned artifact.
#
# (3) is enforced LIVE by the write-fence (out-of-scope creates are reverted) +
# the targeted-edit feedback. (1)+(2) are checked here as a GLOBAL gate AFTER
# implement: an orphan artifact (a generated source file no task owns) is a file
# whose repair scope, responsibility, and write-fence are all undecidable — it sits
# OUTSIDE the contract graph, so the SUT can keep mutating it invisibly (the
# 2026-06-15 codex11 invented an unowned e2e test that re-broke each rerun).
#
# ROLLOUT SAFETY: the global gate defaults to WARN (observe + report, never block)
# because attributing every file to a task is heuristic — a legitimate scaffold or
# config file can read as an orphan. ``adopt-or-reject`` (directory ownership in
# ``build_path_owner_index`` ADOPTS a helper a task wrote under its own output dir;
# the fence REJECTS an out-of-scope create) keeps legitimate helpers owned, so the
# warn list should be small. ``enforce`` is opt-in for projects that want the hard
# invariant. Language-agnostic: the artifact set is the same tracked-source walk the
# fence/fan-out use; no per-language logic here.


@dataclass(frozen=True)
class OrphanArtifact:
    """A generated source artifact with no owning task (the invariant breach)."""

    path: str
    #: why it is flagged (for the warn/enforce message)
    reason: str = "no owning task"


def find_orphan_artifacts(
    index: TaskOutputIndex,
    project_root: Path,
    *,
    extra_owned: Iterable[str] = (),
) -> list[OrphanArtifact]:
    """Find generated source files under ``project_root`` that no task owns.

    A file is an orphan when :meth:`TaskOutputIndex.owner_for` returns ``None`` for
    it AND it is not in ``extra_owned`` (the harness/profile contract escape hatch:
    manifests, scaffolded config, entrypoints a profile owns implicitly). Only
    tracked SOURCE files (``_TS_SOURCE_EXTS``) under the project are considered;
    vendored/build/VCS dirs are skipped. Pure + best-effort: an unreadable tree
    yields an empty list (warn mode must never crash the gate).

    This is the OBSERVATION primitive; the gate decides warn-vs-enforce. It is
    deliberately conservative (source files only, owner-or-extra) so the default
    WARN list stays signal, not noise.
    """
    owned_extra = {_norm(p) for p in extra_owned if _norm(p)}
    orphans: list[OrphanArtifact] = []
    for source_file in _iter_project_source_files(project_root):
        rel = _to_relative(source_file, project_root)
        if not rel or rel in owned_extra:
            continue
        if index.owner_for(rel) is None:
            orphans.append(OrphanArtifact(path=rel))
    return orphans


# ─────────────────────────────────────────────────────────────────────────────
# the public derivation: diagnostics → bounded scope (+ escalation decision)
# ─────────────────────────────────────────────────────────────────────────────


def derive_oracle_rerun_scope(
    *,
    output: str,
    project_root: Path,
    index: TaskOutputIndex,
    rung: str,
    structured_source: StructuredDiagnosticSource | None = None,
    manifest_paths: Sequence[str] = (),
    legacy_broad: bool = False,
) -> ScopeDecision:
    """Derive a bounded rerun scope at ladder ``rung`` from oracle output.

    The full pipeline: parse diagnostics (structured source preferred, else the
    regex layer) → build edges → collect artifact paths → map to owning tasks →
    apply the breadth + wide-fan-out guards → assemble the
    :class:`OracleRerunScope` with its write-fence ``allowed_paths``.

    ``rung`` selects breadth: ``SCOPE_NARROW`` = edge ends only; ``SCOPE_EXPANDED``
    = edge ends + the owning tasks' one-hop neighbours; ``SCOPE_BROAD`` = every
    task. Returns a :class:`ScopeDecision`: a bounded scope, or ``scope=None`` /
    ``force_broad=True`` when the diagnostics admit no usable narrow target.

    ``legacy_broad`` (config ``implement.oracle_legacy_broad_enabled``): when True,
    a wide-fan-out artifact (and ``rung == SCOPE_BROAD``) falls to the LEGACY
    whole-project broad rerun. When False (the default), a wide-fan-out artifact
    instead yields a BROAD-CAMPAIGN scope (a :class:`BroadRepairPlan` the gate
    executes incrementally against the whole-project oracle).
    """
    if rung == SCOPE_BROAD:
        return ScopeDecision(scope=_broad_scope(index, manifest_paths), reason="ladder at broad")

    diagnostics = _collect_diagnostics(output, project_root, structured_source)
    if not diagnostics:
        return ScopeDecision(scope=None, force_broad=True, reason="no parseable diagnostics → broad")

    edges = _edges_from_diagnostics(diagnostics, project_root)
    edge_paths = _dedupe([p for edge in edges for p in edge.paths])
    if not edge_paths:
        return ScopeDecision(scope=None, force_broad=True, reason="no diagnostic paths → broad")

    # Wide-fan-out guard: a public entrypoint / barrel / shared schema with many
    # consumers → broad (chasing a narrow edge would thrash). MEASURED fan-out,
    # not a basename (a tiny index.ts stays scopable). Instead of the legacy
    # whole-project broad, build a BROAD-CAMPAIGN scope: a budgeted residual-
    # coherence plan (supplier-first → residual importers → chunked broad) the gate
    # executes against the whole-project oracle. The legacy whole-project broad is
    # only used when explicitly opted in (``legacy_broad`` arg / config).
    wide = _wide_fanout_path(edge_paths, project_root)
    if wide is not None:
        focus_path, importer_paths = wide
        if legacy_broad:
            return ScopeDecision(
                scope=None,
                force_broad=True,
                reason=(
                    f"wide-fan-out artifact '{focus_path}' "
                    f"(>= {_WIDE_FANOUT_IMPORTER_THRESHOLD} importers) → legacy broad (opted in)"
                ),
            )
        campaign = _build_broad_campaign_scope(
            focus_path=focus_path,
            importer_paths=importer_paths,
            index=index,
            edges=edges,
            manifest_paths=manifest_paths,
        )
        return ScopeDecision(scope=campaign, reason=campaign.detail)

    owners = _owners_for_paths(edge_paths, index)
    if not owners:
        return ScopeDecision(scope=None, force_broad=True, reason="no owning task for diagnostics → broad")

    if rung == SCOPE_EXPANDED:
        owners = _expand_one_hop(owners, edge_paths, project_root, index)

    # Breadth guard: too wide → just go broad.
    if _too_wide(owners, index):
        return ScopeDecision(
            scope=None,
            force_broad=True,
            reason=f"scope {len(owners)} task(s) exceeds max({_MAX_SCOPE_TASKS}, "
            f"{int(_MAX_SCOPE_FRACTION * 100)}% of {len(index.all_task_ids)}) → broad",
        )

    allowed = _allowed_paths(owners, edge_paths, index, manifest_paths)
    scope = OracleRerunScope(
        rung=rung,
        task_ids=tuple(owners),
        allowed_paths=tuple(allowed),
        edges=tuple(edges),
        detail=(
            f"{rung} scope: {len(owners)} task(s) {list(owners)} from "
            f"{len(edges)} diagnostic edge(s) over {len(edge_paths)} path(s)"
        ),
    )
    return ScopeDecision(scope=scope, reason=scope.detail)


def _collect_diagnostics(
    output: str, project_root: Path, structured_source: StructuredDiagnosticSource | None
) -> list[StructuredDiagnostic]:
    """Layer-1 source if supplied, else the layer-2 regex parser."""
    if structured_source is not None:
        try:
            structured = structured_source.diagnostics(output, project_root)
            if structured:
                return structured
        except Exception:  # noqa: BLE001 — a broken layer-1 source must fall back, not abort.
            pass
    return _parse_ts_diagnostics(output, project_root)


def _owners_for_paths(paths: Sequence[str], index: TaskOutputIndex) -> list[str]:
    owners: list[str] = []
    for path in paths:
        owner = index.owner_for(path)
        if owner is not None and owner not in owners:
            owners.append(owner)
    return owners


def _expand_one_hop(
    owners: Sequence[str], edge_paths: Sequence[str], project_root: Path, index: TaskOutputIndex
) -> list[str]:
    """One-hop expansion: add tasks owning files the edge files import.

    The narrow scope was insufficient (the signature survived), so widen by one
    import hop: for each edge file, add the owning tasks of the project files it
    imports. Bounded to ONE hop — still far short of broad.
    """
    expanded = list(owners)
    for path in edge_paths:
        for neighbour in _first_hop_imports(path, project_root):
            owner = index.owner_for(neighbour)
            if owner is not None and owner not in expanded:
                expanded.append(owner)
    return expanded


def _first_hop_imports(path: str, project_root: Path) -> list[str]:
    content = _read((project_root / path).resolve())
    if content is None:
        return []
    base = (project_root / path).resolve().parent
    out: list[str] = []
    for m in _TS_IMPORT_FROM.finditer(content):
        spec = m.group("spec")
        if not spec.startswith("."):
            continue
        target = _resolve_specifier(base, spec, project_root)
        if target is not None:
            out.append(target)
    return _dedupe(out)


def _too_wide(owners: Sequence[str], index: TaskOutputIndex) -> bool:
    total = len(index.all_task_ids) or len(owners)
    threshold = max(_MAX_SCOPE_TASKS, int(_MAX_SCOPE_FRACTION * total))
    return len(owners) > threshold


def _wide_fanout_path(paths: Sequence[str], project_root: Path) -> tuple[str, tuple[str, ...]] | None:
    """First implicated path with wide MEASURED fan-out + its importers, else ``None``.

    Counts, for each implicated path, how many OTHER project source files import
    it (resolving each project import specifier to a file). A path imported by
    ``>= _WIDE_FANOUT_IMPORTER_THRESHOLD`` files is a barrel/entrypoint/shared
    module whose regeneration touches many consumers — go broad. Returns the
    ``(focus_path, importer_paths)`` tuple for the FIRST such path (so the
    broad-campaign plan can target the supplier AND its actual importers), or
    ``None`` when no implicated path is wide. Filesystem walk is bounded to tracked
    source files and skips vendored dirs.
    """
    targets = {_norm(p) for p in paths if _norm(p)}
    if not targets:
        return None
    importers: dict[str, list[str]] = {t: [] for t in targets}
    for source_file in _iter_project_source_files(project_root):
        rel = _to_relative(source_file, project_root)
        if not rel or rel in targets:
            continue  # a file's import of itself does not count toward its fan-in
        content = _read(source_file)
        if content is None:
            continue
        base = source_file.parent
        for m in _TS_IMPORT_FROM.finditer(content):
            spec = m.group("spec")
            if not spec.startswith("."):
                continue
            resolved = _resolve_specifier(base, spec, project_root)
            if resolved in importers and rel not in importers[resolved]:
                importers[resolved].append(rel)
    for target in targets:  # deterministic order = the implicated-path order
        if len(importers[target]) >= _WIDE_FANOUT_IMPORTER_THRESHOLD:
            return target, tuple(importers[target])
    return None


def importers_of(path: str, project_root: Path) -> tuple[str, ...]:
    """Project source files that import ``path`` (resolved), in walk order.

    The public, single-path counterpart of :func:`_wide_fanout_path`'s inner count
    — used by the broad-campaign derivation to find the consumer tasks of a
    wide-fan-out supplier, and re-usable by a Go/Rust adapter. Pure + best-effort:
    an unreadable tree yields ``()``.
    """
    target = _norm(path)
    if not target:
        return ()
    out: list[str] = []
    for source_file in _iter_project_source_files(project_root):
        rel = _to_relative(source_file, project_root)
        if not rel or rel == target:
            continue
        content = _read(source_file)
        if content is None:
            continue
        base = source_file.parent
        for m in _TS_IMPORT_FROM.finditer(content):
            spec = m.group("spec")
            if not spec.startswith("."):
                continue
            if _resolve_specifier(base, spec, project_root) == target and rel not in out:
                out.append(rel)
    return tuple(out)


def _task_graph_longest_chain_ranks(index: TaskOutputIndex) -> dict[str, int]:
    """Cycle-safe longest-chain rank per task over ``index.dependencies``.

    ``rank(t) = 1 + max(rank(d) for d in t.dependencies)`` (0 when edge-less). A
    dependency cycle is broken by a stack guard — the re-entrant edge contributes 0
    (no infinite recursion, no raise), so ranking terminates deterministically; the
    cycle's tasks still receive finite ranks from their acyclic edges. An edge to an
    unknown task id is ignored. Pure.
    """
    decl = {tid: i for i, tid in enumerate(index.all_task_ids)}
    cache: dict[str, int] = {}

    def _rank(tid: str, stack: frozenset[str]) -> int:
        cached = cache.get(tid)
        if cached is not None:
            return cached
        if tid in stack:
            return 0  # cycle guard: degrade to the declaration-order tiebreak
        best = 0
        for dep in index.dependencies.get(tid, ()):
            if dep == tid or dep not in decl:
                continue  # self/unknown edge contributes nothing
            best = max(best, _rank(dep, stack | {tid}) + 1)
        cache[tid] = best
        return best

    return {tid: _rank(tid, frozenset()) for tid in index.all_task_ids}


def task_dependency_order(task_ids: Sequence[str], index: TaskOutputIndex) -> tuple[str, ...]:
    """Order ``task_ids`` producer-first over the planner's task ``dependencies``.

    PRIMARY rank (FIX-1, Fable5 ts-v9 ruling): the cycle-safe longest-chain over
    ``index.dependencies`` (:func:`_task_graph_longest_chain_ranks`) — the SAME
    production rank :func:`_topologically_order_implement_tasks` now uses for
    first-pass order, so a repair rerun regenerates producers before consumers
    instead of re-walking the design-DAG-inverted order that regenerated the
    barrel before its producers.

    TIEBREAK / SOLE FALLBACK: the shipped declaration order (``all_task_ids``
    position). When NO task declares a ``dependencies`` edge (legacy index) every
    rank is 0 and the order collapses to declaration order — byte-identical to the
    shipped behavior. Unknown ids (not in the index) are appended last in their
    given order. Pure + deterministic; de-duplicated.
    """
    decl = {tid: i for i, tid in enumerate(index.all_task_ids)}
    ranks = _task_graph_longest_chain_ranks(index)
    wanted = _dedupe(task_ids)
    known = [t for t in wanted if t in decl]
    unknown = [t for t in wanted if t not in decl]
    known.sort(key=lambda t: (ranks.get(t, 0), decl[t]))
    return tuple(known + unknown)


#: Source-tree dirs the fan-out walk skips (vendored deps / VCS / build output).
_FANOUT_SKIP_DIRS = frozenset(
    {"node_modules", ".git", ".codd", "dist", "build", "__pycache__", ".pytest_cache"}
)


def _iter_project_source_files(project_root: Path):
    """Yield tracked TS/JS source files under ``project_root`` (vendored skipped)."""
    import os

    root = project_root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _FANOUT_SKIP_DIRS]
        for name in filenames:
            if PurePosixPath(name).suffix in _TS_SOURCE_EXTS:
                yield Path(dirpath) / name


def _allowed_paths(
    owners: Sequence[str],
    edge_paths: Sequence[str],
    index: TaskOutputIndex,
    manifest_paths: Sequence[str],
) -> list[str]:
    """The write-fence: the only paths the scoped rerun may create/modify.

    = the scoped tasks' output paths (exact files + their directories, so a task
    that owns ``src/`` may write any file under it) + the diagnostic-derived
    importer/exporter candidates + the needed manifest/config. Anything outside
    this set the caller reverts or hard-fails.
    """
    allowed: list[str] = []
    owner_set = set(owners)
    for file_path, task_id in index.exact.items():
        if task_id in owner_set:
            allowed.append(file_path)
    for directory, task_id in index.dirs:
        if task_id in owner_set:
            allowed.append(directory)
    allowed.extend(edge_paths)
    allowed.extend(_norm(p) for p in manifest_paths)
    return _dedupe([p for p in allowed if p])


def _broad_scope(index: TaskOutputIndex, manifest_paths: Sequence[str]) -> OracleRerunScope:
    """The broad fallback: every task, no write-fence restriction beyond manifest.

    Broad allows ANY path (the legacy behaviour). ``allowed_paths`` is left
    EMPTY, which the caller interprets as "no fence" (broad rerun regenerates the
    whole build), distinguishing it from a scoped rerun whose fence is non-empty.
    """
    return OracleRerunScope(
        rung=SCOPE_BROAD,
        task_ids=tuple(index.all_task_ids),
        allowed_paths=(),  # empty ⇒ no fence (broad regenerates everything)
        detail=f"broad scope: all {len(index.all_task_ids)} task(s)",
    )


def _build_broad_campaign_scope(
    *,
    focus_path: str,
    importer_paths: Sequence[str],
    index: TaskOutputIndex,
    edges: Sequence[DiagnosticEdge],
    manifest_paths: Sequence[str],
) -> OracleRerunScope:
    """A broad-RUNG scope whose EXECUTION is the budgeted residual-coherence campaign.

    Builds the :class:`BroadRepairPlan` for a wide-fan-out ``focus_path``:
      * supplier task(s) = owner(s) of the focus artifact (the exporter end).
      * importer task(s) = owner tasks of the ``importer_paths`` (the upfront
        residual candidate set; refined per-recheck by the gate).
      * phases skeleton: supplier_first (fenced to supplier outputs + focus +
        manifest) → residual_importers (fenced to importer outputs + manifest) →
        chunked_broad (all tasks, dependency order, manifest fence). The gate
        re-derives the LIVE residual-importer scope from the current diagnostics on
        each recheck; the skeleton's importer phase is the fallback when the live
        derivation yields nothing.

    The returned scope's ``rung`` is :data:`SCOPE_BROAD` (so existing
    ``is_broad()`` callers still see broad) and its ``repair_plan`` is set (so the
    gate branches to the incremental campaign and the pipeline fences each phase).
    """
    supplier_ids = tuple(_dedupe(_owners_for_paths([focus_path], index)))
    importer_ids = tuple(
        t for t in _dedupe(_owners_for_paths(importer_paths, index)) if t not in supplier_ids
    )

    # supplier_first: re-implement only the supplier task(s), fenced to their
    # outputs + the focus artifact + manifest. This is the exporter-surface fix.
    supplier_allowed = _allowed_paths(supplier_ids, [focus_path], index, manifest_paths)
    supplier_scope = OracleRerunScope(
        rung=SCOPE_BROAD,
        task_ids=supplier_ids,
        allowed_paths=tuple(supplier_allowed),
        edges=tuple(edges),
        detail=(
            f"broad-campaign supplier_first: {len(supplier_ids)} supplier task(s) "
            f"{list(supplier_ids)} for focus '{focus_path}'"
        ),
    )
    supplier_phase = OracleRepairPhase(
        phase=PHASE_SUPPLIER_FIRST,
        scope=supplier_scope,
        focus_paths=(focus_path,),
        reason=f"repair shared exporter '{focus_path}' first (wide fan-out)",
    )

    # residual_importers: the upfront candidate importer owners, fenced to their
    # outputs + manifest. (The gate refines this per-recheck from live residual.)
    importer_allowed = _allowed_paths(importer_ids, importer_paths, index, manifest_paths)
    importer_scope = OracleRerunScope(
        rung=SCOPE_BROAD,
        task_ids=importer_ids,
        allowed_paths=tuple(importer_allowed),
        edges=tuple(edges),
        detail=(
            f"broad-campaign residual_importers: {len(importer_ids)} importer task(s) "
            f"{list(importer_ids)}"
        ),
    )
    importer_phase = OracleRepairPhase(
        phase=PHASE_RESIDUAL_IMPORTERS,
        scope=importer_scope,
        focus_paths=tuple(_dedupe(importer_paths)),
        reason="repair the importers the whole-project oracle still proves broken",
    )

    # chunked_broad: every task, dependency-ordered, one pass. Fenced only to the
    # union of all known outputs + manifest (effectively the whole tree, but still
    # records an allowed set so the pipeline keeps the fence machinery uniform).
    all_ordered = task_dependency_order(index.all_task_ids, index)
    chunked_allowed = _allowed_paths(all_ordered, [], index, manifest_paths)
    chunked_scope = OracleRerunScope(
        rung=SCOPE_BROAD,
        task_ids=all_ordered,
        allowed_paths=tuple(chunked_allowed),
        edges=tuple(edges),
        detail=f"broad-campaign chunked_broad: all {len(all_ordered)} task(s), dependency order",
    )
    chunked_phase = OracleRepairPhase(
        phase=PHASE_CHUNKED_BROAD,
        scope=chunked_scope,
        focus_paths=(),
        reason="last-resort full dependency-ordered pass (1×)",
    )

    plan = BroadRepairPlan(
        focus_paths=(focus_path,),
        supplier_task_ids=supplier_ids,
        importer_task_ids=importer_ids,
        phases=(supplier_phase, importer_phase, chunked_phase),
    )
    return OracleRerunScope(
        rung=SCOPE_BROAD,
        task_ids=tuple(_dedupe([*supplier_ids, *importer_ids])),
        allowed_paths=tuple(supplier_allowed),  # the FIRST phase's fence (informational)
        edges=tuple(edges),
        detail=(
            f"broad-campaign for wide-fan-out '{focus_path}' "
            f"({len(importer_paths)} importer(s)): supplier_first → residual_importers "
            f"→ chunked_broad (budgeted residual coherence)"
        ),
        repair_plan=plan,
    )


def derive_residual_importer_scope(
    *,
    output: str,
    project_root: Path,
    index: TaskOutputIndex,
    exclude_task_ids: Sequence[str] = (),
    manifest_paths: Sequence[str] = (),
    structured_source: StructuredDiagnosticSource | None = None,
    chunk_size: int | None = None,
) -> OracleRerunScope | None:
    """A fenced scope over the owner tasks of the CURRENT residual diagnostics.

    Called by the broad campaign AFTER the supplier phase: parse the diagnostics
    the whole-project oracle STILL reports, map every implicated path to its owner
    task, drop ``exclude_task_ids`` (the supplier already repaired — re-running it
    would risk the exporter↔importer oscillation), order the residual owners in
    dependency order, take at most ``chunk_size`` of them (the design's
    ``oracle_residual_chunk_size`` — a bounded importer chunk per recheck so each
    rerun's surface + wall-clock stays small; ``None``/``<=0`` ⇒ no limit = all
    residual owners in one phase), and return a fenced :class:`OracleRerunScope`
    over just those residual importer owner tasks. The fence = those tasks' outputs
    + the residual paths they own + manifest, so the residual phase is a localized
    reconcile. Returns ``None`` when no residual owner remains (e.g. the residual is
    only in the excluded supplier, or unparseable) — the caller advances the phase.

    Pure (no compiler run) + best-effort: a parse failure yields ``None``.
    """
    diagnostics = _collect_diagnostics(output, project_root, structured_source)
    if not diagnostics:
        return None
    edges = _edges_from_diagnostics(diagnostics, project_root)
    edge_paths = _dedupe([p for edge in edges for p in edge.paths])
    if not edge_paths:
        return None
    excluded = set(exclude_task_ids)
    owners = [t for t in _owners_for_paths(edge_paths, index) if t not in excluded]
    if not owners:
        return None
    # Dependency-order the residual owners (suppliers before consumers) and take a
    # bounded chunk so a large residual is repaired chunk-by-chunk across rechecks.
    owners = list(task_dependency_order(owners, index))
    if chunk_size is not None and chunk_size > 0:
        owners = owners[:chunk_size]
    # Fence to ONLY the chunk's own paths (so an out-of-chunk residual file written
    # by this rerun is reverted — the chunk stays local).
    chunk_paths = [p for p in edge_paths if index.owner_for(p) in set(owners)]
    allowed = _allowed_paths(owners, chunk_paths or edge_paths, index, manifest_paths)
    return OracleRerunScope(
        rung=SCOPE_BROAD,
        task_ids=tuple(owners),
        allowed_paths=tuple(allowed),
        edges=tuple(edges),
        detail=(
            f"broad-campaign residual_importers (live): {len(owners)} task(s) "
            f"{list(owners)} from {len(edge_paths)} residual path(s)"
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# small pure helpers
# ─────────────────────────────────────────────────────────────────────────────


def _task_id(task: object) -> str | None:
    val = getattr(task, "task_id", None)
    return str(val) if val else None


def _task_output_paths(task: object) -> tuple[str, ...] | None:
    val = getattr(task, "output_paths", None)
    if not val:
        return None
    return tuple(str(p) for p in val)


def _task_dependencies(task: object) -> tuple[str, ...]:
    """The task's planner task-level ``dependencies`` (production-graph edges).

    Best-effort: a task object without the attribute (legacy/configured refs)
    yields ``()`` so :func:`task_dependency_order` falls back to declaration order.
    """
    val = getattr(task, "dependencies", None)
    if not val:
        return ()
    return tuple(str(d).strip() for d in val if str(d).strip())


def _looks_like_file(norm: str) -> bool:
    """A path with a file extension is treated as a file; else a directory."""
    return bool(PurePosixPath(norm).suffix)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _norm(rel: str | None) -> str:
    if not rel:
        return ""
    return str(rel).strip().replace("\\", "/").strip("/")


def _project_relative(raw: str, project_root: Path) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        resolved = (project_root / text).resolve()
        return resolved.relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(text.replace("\\", "/")).as_posix()


def _to_relative(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
