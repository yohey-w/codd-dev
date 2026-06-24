"""Requirement-to-operation reconciliation for ``codd doctor``.

Every operation-driven coverage axis (operational E2E obligations, action
outcome coverage, capability completeness, surface reconciliation) iterates the
*declared* ``operation_flow`` universe. A behaviour that is **written in the
requirement documents** but **never lifted into ``operation_flow``** is
therefore structurally invisible: no obligation is derived, no audit row
exists, and a placeholder implementation can stay green forever.

The existing reconciliation checks cover two of the three edges of the
requirements/operations/source triangle:

* ``surface_reconciliation`` — implemented source -> declared operations
  ("implemented but undeclared").
* ``orphan_cover_markers`` — test markers -> declared operations.

This module adds the missing edge: requirement documents -> declared
operations ("required but undeclared"). Two advisory checks:

**Check A — dangling operation references (deterministic).** Requirement docs
may reference operations explicitly as ``operation_flow.<id>``. A reference
whose ``<id>`` resolves to no declared operation is reported. A reference is
an explicit claim, so this check has no false-positive surface.

**Check B — unreconciled requirement units (conservative heuristic).**
Markdown table rows in requirement documents are treated as *requirement
units* (one row ~ one declarable behaviour). A unit is *reconciled* when any
deterministic, language-neutral anchor ties it to the declared universe:

1. an explicit ``operation_flow.<id>`` reference that resolves,
2. an out-of-scope marker (built-in or configured),
3. a route path in the unit text matching a declared operation route, or
4. a term in the unit text matching a declared operation id/verb/target token
   (or a ``runtime.action_outcome_targets`` token).

Units with no anchor are reported so the project either declares the missing
operation or marks the unit out of scope. To prevent a false-positive flood,
Check B audits only tables the project itself marked as operation-traceable
(tables already containing at least one ``operation_flow.<id>`` reference) or
tables under section headings explicitly listed in
``requirement_reconciliation.sections``. Everything else stays silent.

Known limitation (deliberate, generality-first): unit text written entirely in
a language that shares no tokens with operation identifiers cannot be matched
lexically; such units rely on explicit references or out-of-scope markers.
That is by design — the check nudges projects toward explicit
requirement-to-operation traceability instead of guessing semantics, which
would require an LLM and make the check non-deterministic.

Both checks are advisory-only (``codd doctor`` never fails on warnings), are
dormant when no operations are declared, and are opt-out via
``requirement_reconciliation.enabled: false`` in codd.yaml. No framework or
project vocabulary appears in this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from codd.action_outcome import _normalize_token, canonical_action_verb
from codd.path_safety import resolve_project_path
from codd.requirements_meta import operation_flow_operations


# codd.yaml mapping that tunes this check.
SETTINGS_KEY = "requirement_reconciliation"

# Explicit requirement-to-operation reference, e.g. ``operation_flow.create_item``.
_OPERATION_REFERENCE_RE = re.compile(r"\boperation_flow\.([A-Za-z0-9][A-Za-z0-9_-]*)")

# Markdown structure.
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}.*$")

# Route-shaped path inside unit text (language-neutral anchor). The lookbehind
# blocks word-internal slashes ("HTML/Markdown", CJK compounds) because ``\w``
# is Unicode-aware.
_ROUTE_IN_TEXT_RE = re.compile(r"(?<!\w)/[A-Za-z][A-Za-z0-9_\-{}\[\]:.]*(?:/[A-Za-z0-9_\-{}\[\]:.]+)*")

# Route parameter spellings are normalized to a common placeholder so
# ``/items/:id``, ``/items/[id]`` and ``/items/{id}`` reconcile.
_ROUTE_PARAM_RE = re.compile(r"\[[^\]/]+\]|:[A-Za-z0-9_]+|\{[^}/]+\}")

# ASCII-ish term inside unit text. Three characters minimum keeps initials and
# markup noise out.
_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")

# Generic words that must not count as reconciliation evidence on their own.
_TERM_STOPWORDS = frozenset(
    {
        "and",
        "are",
        "can",
        "etc",
        "for",
        "from",
        "must",
        "not",
        "operation",
        "operation_flow",
        "per",
        "shall",
        "should",
        "the",
        "that",
        "this",
        "via",
        "with",
    }
)

# Built-in out-of-scope markers. Natural-language phrases (not framework or
# project vocabulary); a project replaces them via ``out_of_scope_markers``.
DEFAULT_OUT_OF_SCOPE_MARKERS: tuple[str, ...] = (
    "out of scope",
    "out-of-scope",
    "スコープ外",
    "将来対応",
    "対象外",
)

# Upper bound for individual unit warnings before aggregation.
DEFAULT_MAX_UNIT_WARNINGS = 30


@dataclass(frozen=True)
class RequirementUnit:
    """One declarable behaviour written in a requirement document."""

    source: str
    section: str
    label: str
    text: str


@dataclass(frozen=True)
class DanglingOperationReference:
    """An ``operation_flow.<id>`` reference that resolves to no declared operation."""

    source: str
    reference: str

    @property
    def message(self) -> str:
        return (
            f"[dangling_requirement_reference] `{self.source}` references "
            f"`operation_flow.{self.reference}` but no operation with id "
            f"`{self.reference}` is declared in any operation_flow. Declare the "
            f"operation or fix the reference so requirement-to-operation "
            f"traceability stays machine-checkable."
        )


@dataclass(frozen=True)
class UnreconciledUnit:
    """A requirement unit with no anchor into the declared operation universe."""

    unit: RequirementUnit

    @property
    def message(self) -> str:
        section = f" (section `{self.unit.section}`)" if self.unit.section else ""
        return (
            f"[requirement_reconciliation] Requirement unit `{self.unit.label}` in "
            f"`{self.unit.source}`{section} reconciles with no declared operation: no "
            f"`operation_flow.<id>` reference resolves, no declared route appears in the "
            f"unit text, and no unit term matches a declared operation id/verb/target. "
            f"Operation-driven coverage (E2E obligations, action outcome, capability "
            f"completeness) is structurally blind to this behaviour until it is declared. "
            f"Declare the operation in operation_flow and reference it as "
            f"`operation_flow.<id>` in the unit text, mark the unit out of scope, or tune "
            f"`{SETTINGS_KEY}` in codd.yaml."
        )


@dataclass(frozen=True)
class ReconciliationSettings:
    enabled: bool = True
    docs: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    out_of_scope_markers: tuple[str, ...] = DEFAULT_OUT_OF_SCOPE_MARKERS
    max_unit_warnings: int = DEFAULT_MAX_UNIT_WARNINGS


@dataclass(frozen=True)
class _Table:
    """A contiguous Markdown table with its enclosing section heading."""

    section: str
    rows: tuple[str, ...] = field(default_factory=tuple)


def requirement_reconciliation_settings(config: Mapping[str, Any]) -> ReconciliationSettings:
    """Resolve check settings from project ``config``.

    Defaults: enabled (advisory only). The settings key is absent by default,
    so existing projects keep the built-in behaviour: dangling-reference
    reconciliation plus unit reconciliation restricted to tables the project
    already marked with ``operation_flow.<id>`` references. ``sections`` widens
    the unit scope; a non-empty ``out_of_scope_markers`` replaces the built-in
    marker list.
    """

    settings = config.get(SETTINGS_KEY) if isinstance(config, Mapping) else None
    if not isinstance(settings, Mapping):
        return ReconciliationSettings()

    markers = _string_tuple(settings.get("out_of_scope_markers"))
    raw_max = settings.get("max_unit_warnings")
    try:
        max_warnings = int(raw_max) if raw_max is not None else DEFAULT_MAX_UNIT_WARNINGS
    except (TypeError, ValueError):
        max_warnings = DEFAULT_MAX_UNIT_WARNINGS
    return ReconciliationSettings(
        enabled=bool(settings.get("enabled", True)),
        docs=_string_tuple(settings.get("docs")),
        sections=_string_tuple(settings.get("sections")),
        out_of_scope_markers=markers or DEFAULT_OUT_OF_SCOPE_MARKERS,
        max_unit_warnings=max(1, max_warnings),
    )


def discover_requirement_docs(project_root: Path, config: Mapping[str, Any]) -> list[Path]:
    """Return requirement documents to reconcile.

    Configured paths (files or directories) win; the default mirrors the
    discovery used by the requirement completeness auditor:
    ``docs/requirements/**/*.md`` plus conventional top-level files.
    """

    root = Path(project_root)
    settings = requirement_reconciliation_settings(config)
    candidates: list[Path] = []
    if settings.docs:
        for raw in settings.docs:
            # ``requirement_reconciliation.docs`` is user-controllable (codd.yaml);
            # an absolute/``../`` value or an in-root symlink whose target escapes
            # the tree must not surface a doc whose contents become reconciliation
            # evidence. Jail the configured path, then re-confine each rglob match
            # (rglob follows symlinks, so an in-root *.md may point outside root).
            path = resolve_project_path(root, raw)
            if path is None:
                continue
            if path.is_dir():
                candidates.extend(
                    confined
                    for md_path in sorted(path.rglob("*.md"))
                    if (confined := resolve_project_path(root, md_path)) is not None
                )
            elif path.is_file():
                candidates.append(path)
        return _unique_paths(candidates)

    req_dir = root / "docs" / "requirements"
    if req_dir.exists():
        # rglob follows symlinks: re-confine each match so an in-root *.md
        # symlinked outside the root is not surfaced as a requirement doc.
        candidates.extend(
            confined
            for md_path in sorted(req_dir.rglob("*.md"))
            if (confined := resolve_project_path(root, md_path)) is not None
        )
    for filename in ("docs/requirements.md", "REQUIREMENTS.md", "requirements.md"):
        path = root / filename
        if resolve_project_path(root, path) is not None and path.is_file():
            candidates.append(path)
    return _unique_paths(candidates)


def declared_operation_ids(flows: Iterable[tuple[str, Any]]) -> frozenset[str]:
    """Normalized ids of every declared operation across ``flows``."""

    ids: set[str] = set()
    for _source, flow in flows:
        for operation in operation_flow_operations(flow):
            token = _normalize_token(operation.get("id"))
            if token:
                ids.add(token)
    return frozenset(ids)


def declared_operation_tokens(flows: Iterable[tuple[str, Any]]) -> frozenset[str]:
    """Lexical tokens (id/verb/target words + canonical verbs) of declared operations.

    Deliberately generous on the declared side, mirroring
    ``surface_reconciliation``: any term overlap counts as reconciled, so the
    unit check only fires when a requirement unit shares *nothing* with the
    declared universe.
    """

    tokens: set[str] = set()
    for _source, flow in flows:
        for operation in operation_flow_operations(flow):
            for key in ("id", "verb", "target"):
                raw = operation.get(key)
                token = _normalize_token(raw)
                if token:
                    tokens.add(token)
                    tokens.update(part for part in token.split("_") if len(part) >= 3)
                canonical = canonical_action_verb(raw)
                if canonical:
                    tokens.add(canonical)
    return frozenset(tokens - _TERM_STOPWORDS)


def declared_operation_routes(flows: Iterable[tuple[str, Any]]) -> frozenset[str]:
    """Normalized route paths declared by operations across ``flows``."""

    routes: set[str] = set()
    for _source, flow in flows:
        for operation in operation_flow_operations(flow):
            for key in ("route", "routes"):
                raw = operation.get(key)
                values = raw if isinstance(raw, (list, tuple)) else [raw]
                for value in values:
                    if not isinstance(value, str):
                        continue
                    normalized = _normalize_route(value)
                    if normalized:
                        routes.add(normalized)
    return frozenset(routes)


def detect_dangling_operation_references(
    doc_texts: Iterable[tuple[str, str]],
    declared_ids: frozenset[str],
) -> tuple[DanglingOperationReference, ...]:
    """Check A: explicit references that resolve to no declared operation."""

    dangling: list[DanglingOperationReference] = []
    seen: set[tuple[str, str]] = set()
    for source, text in doc_texts:
        for match in _OPERATION_REFERENCE_RE.finditer(text):
            reference = match.group(1)
            if _normalize_token(reference) in declared_ids:
                continue
            key = (source, reference)
            if key in seen:
                continue
            seen.add(key)
            dangling.append(DanglingOperationReference(source=source, reference=reference))
    return tuple(dangling)


def parse_requirement_units(
    text: str,
    source: str,
    *,
    sections: tuple[str, ...] = (),
) -> list[RequirementUnit]:
    """Extract auditable requirement units (table rows) from a Markdown document.

    A table is auditable when it already contains an ``operation_flow.<id>``
    reference (the project marked it operation-traceable) or its enclosing
    section heading matches a configured ``sections`` substring. Header and
    separator rows are skipped.
    """

    units: list[RequirementUnit] = []
    for table in _parse_tables(text):
        if not _table_in_scope(table, sections):
            continue
        rows = list(table.rows)
        if len(rows) >= 2 and _TABLE_SEPARATOR_RE.match(_strip_pipes(rows[1])):
            rows = rows[2:]  # drop header + separator
        for row in rows:
            cells = _row_cells(row)
            if not cells:
                continue
            label = _clean_label(cells[0]) or _clean_label(" ".join(cells))
            units.append(
                RequirementUnit(
                    source=source,
                    section=table.section,
                    label=label,
                    text=" ".join(cells),
                )
            )
    return units


def detect_unreconciled_units(
    doc_texts: Iterable[tuple[str, str]],
    flows: Iterable[tuple[str, Any]],
    *,
    sections: tuple[str, ...] = (),
    out_of_scope_markers: tuple[str, ...] = DEFAULT_OUT_OF_SCOPE_MARKERS,
    extra_tokens: frozenset[str] = frozenset(),
) -> tuple[UnreconciledUnit, ...]:
    """Check B: in-scope requirement units with no anchor to a declared operation."""

    flows = list(flows)
    declared_ids = declared_operation_ids(flows)
    declared_tokens = declared_operation_tokens(flows) | extra_tokens
    declared_routes = declared_operation_routes(flows)
    markers = tuple(marker.lower() for marker in out_of_scope_markers if marker)

    unreconciled: list[UnreconciledUnit] = []
    for source, text in doc_texts:
        for unit in parse_requirement_units(text, source, sections=sections):
            if _unit_is_reconciled(unit, declared_ids, declared_tokens, declared_routes, markers):
                continue
            unreconciled.append(UnreconciledUnit(unit=unit))
    return tuple(unreconciled)


def requirement_reconciliation_warnings(
    doc_texts: Iterable[tuple[str, str]],
    flows: Iterable[tuple[str, Any]],
    config: Mapping[str, Any],
    *,
    runtime_tokens: frozenset[str] = frozenset(),
) -> list[str]:
    """Advisory warnings for requirement-to-operation reconciliation.

    Dormant when the project declares no operations: a project without
    ``operation_flow`` has not opted into operation-driven coverage, so there
    is no declared universe to reconcile against (mirrors
    ``surface_reconciliation``).
    """

    settings = requirement_reconciliation_settings(config)
    if not settings.enabled:
        return []
    flows = list(flows)
    if not any(operation_flow_operations(flow) for _source, flow in flows):
        return []
    doc_texts = list(doc_texts)

    declared_ids = declared_operation_ids(flows)
    messages = [
        dangling.message
        for dangling in detect_dangling_operation_references(doc_texts, declared_ids)
    ]

    unreconciled = detect_unreconciled_units(
        doc_texts,
        flows,
        sections=settings.sections,
        out_of_scope_markers=settings.out_of_scope_markers,
        extra_tokens=frozenset(runtime_tokens) - _TERM_STOPWORDS,
    )
    shown = unreconciled[: settings.max_unit_warnings]
    messages.extend(item.message for item in shown)
    overflow = len(unreconciled) - len(shown)
    if overflow > 0:
        messages.append(
            f"[requirement_reconciliation] ...and {overflow} more unreconciled requirement "
            f"unit(s). Raise `{SETTINGS_KEY}.max_unit_warnings` in codd.yaml to list all."
        )
    return messages


# --- helpers -----------------------------------------------------------------


def _unit_is_reconciled(
    unit: RequirementUnit,
    declared_ids: frozenset[str],
    declared_tokens: frozenset[str],
    declared_routes: frozenset[str],
    markers: tuple[str, ...],
) -> bool:
    lowered = unit.text.lower()

    # 1. Explicit resolving reference.
    for match in _OPERATION_REFERENCE_RE.finditer(unit.text):
        if _normalize_token(match.group(1)) in declared_ids:
            return True

    # 2. Explicit out-of-scope declaration.
    if any(marker in lowered for marker in markers):
        return True

    # 3. Route anchor.
    for route_match in _ROUTE_IN_TEXT_RE.finditer(unit.text):
        if _normalize_route(route_match.group(0)) in declared_routes:
            return True

    # 4. Term overlap with the declared universe.
    for term in _TERM_RE.findall(unit.text):
        token = _normalize_token(term)
        if not token or token in _TERM_STOPWORDS:
            continue
        candidates = [token, *(part for part in token.split("_") if len(part) >= 3)]
        if any(candidate in declared_tokens for candidate in candidates):
            return True
        canonical = canonical_action_verb(token)
        if canonical and canonical in declared_tokens:
            return True
    return False


def _parse_tables(text: str) -> list[_Table]:
    tables: list[_Table] = []
    section = ""
    current_rows: list[str] = []
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            if current_rows:
                tables.append(_Table(section=section, rows=tuple(current_rows)))
                current_rows = []
            section = heading.group("title").strip()
            continue
        if _TABLE_ROW_RE.match(line):
            current_rows.append(line)
            continue
        if current_rows:
            tables.append(_Table(section=section, rows=tuple(current_rows)))
            current_rows = []
    if current_rows:
        tables.append(_Table(section=section, rows=tuple(current_rows)))
    return [table for table in tables if len(table.rows) >= 2]


def _table_in_scope(table: _Table, sections: tuple[str, ...]) -> bool:
    table_text = "\n".join(table.rows)
    if _OPERATION_REFERENCE_RE.search(table_text):
        return True
    if not sections:
        return False
    heading = table.section.lower()
    return any(pattern.lower() in heading for pattern in sections if pattern)


def _row_cells(row: str) -> list[str]:
    cells = [cell.strip() for cell in _strip_pipes(row).split("|")]
    return [cell for cell in cells if cell]


def _strip_pipes(row: str) -> str:
    return row.strip().strip("|")


def _clean_label(cell: str) -> str:
    cleaned = re.sub(r"[*_`]+", "", cell).strip()
    # Drop leading non-word decoration (emoji bullets etc.) but keep CJK text.
    cleaned = re.sub(r"^[^\w(/\[]+", "", cleaned).strip()
    return cleaned


def _normalize_route(route: str) -> str:
    normalized = route.strip().lower().rstrip("/.,;:")
    if not normalized.startswith("/") or len(normalized) < 2:
        return ""
    return _ROUTE_PARAM_RE.sub("{}", normalized)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        cleaned = value.strip()
        return (cleaned,) if cleaned else ()
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return tuple(items)
    return ()


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique
