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
    "DiagnosticEdge",
    "OracleRerunScope",
    "ScopeDecision",
    "ScopeEscalation",
    "StructuredDiagnostic",
    "StructuredDiagnosticSource",
    "TaskOutputIndex",
    "build_path_owner_index",
    "derive_oracle_rerun_scope",
    "diagnostic_signature",
]


# ── escalation ladder rungs (order matters; index = breadth) ──────────────────
SCOPE_NARROW = "narrow"  # both ends of the broken demand edge(s)
SCOPE_EXPANDED = "expanded"  # narrow + one import-hop neighbours of the edge ends
SCOPE_BROAD = "broad"  # every implement task (the legacy behaviour, now a fallback)
SCOPE_LADDER: tuple[str, ...] = (SCOPE_NARROW, SCOPE_EXPANDED, SCOPE_BROAD)


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

    def is_broad(self) -> bool:
        return self.rung == SCOPE_BROAD


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
    gen = generated_files or {}
    cfg_paths = config_output_paths or {}

    for task in tasks:
        task_id = _task_id(task)
        if task_id is None:
            continue
        all_ids.append(task_id)
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
        exact=exact, dirs=tuple(dirs_sorted), all_task_ids=tuple(dict.fromkeys(all_ids))
    )


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
    # not a basename (a tiny index.ts stays scopable).
    wide = _wide_fanout_path(edge_paths, project_root)
    if wide is not None:
        return ScopeDecision(
            scope=None,
            force_broad=True,
            reason=f"wide-fan-out artifact '{wide}' (>= {_WIDE_FANOUT_IMPORTER_THRESHOLD} importers) → broad",
        )

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


def _wide_fanout_path(paths: Sequence[str], project_root: Path) -> str | None:
    """Return the first implicated path with wide MEASURED fan-out, else ``None``.

    Counts, for each implicated path, how many OTHER project source files import
    it (resolving each project import specifier to a file). A path imported by
    ``>= _WIDE_FANOUT_IMPORTER_THRESHOLD`` files is a barrel/entrypoint/shared
    module whose regeneration touches many consumers — go broad. Filesystem walk
    is bounded to tracked source files and skips vendored dirs.
    """
    targets = {_norm(p) for p in paths if _norm(p)}
    if not targets:
        return None
    counts: dict[str, int] = {t: 0 for t in targets}
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
            if resolved in counts:
                counts[resolved] += 1
    for target, n in counts.items():
        if n >= _WIDE_FANOUT_IMPORTER_THRESHOLD:
            return target
    return None


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
