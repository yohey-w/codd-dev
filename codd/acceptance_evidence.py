"""Acceptance-criterion → evidence binding (the acceptance-evidence invariant).

CoDD already reconciles *design* against *implementation* (implementation
coverage, source completeness) and *test documents* against *test markers*
(the verifiable-behavior audit). The edge it did **not** reconcile is the one a
customer actually signs: the **acceptance criterion** written in the
requirement document.

The invariant this module exists to make checkable:

    For every requirement R and every acceptance criterion AC of R there is
    evidence E such that

      (a) E is machine-executed (or, when it cannot be, an owner-attributed
          ``manual`` record),
      (b) E runs through the SHIPPED PATH of R — the code reachable from the
          entry point a user actually touches,
      (c) E's checked values reference AC's NAMED PARAMETERS, not a re-typed
          literal, and
      (d) E is bound to the content of the implementation it verified, so a
          changed implementation invalidates stale evidence.

    If any of the four is missing, verification is RED. "Nothing declared, so
    a one-line notice and pass" does not exist.

This module is the shared *parser and resolver*; the gate is the
``acceptance_evidence`` DAG check. Both are deliberately free of project,
framework and language literals:

* acceptance criteria are read from the SAME requirement documents and the
  SAME in-scope table rule ``requirement_reconciliation`` already uses (a
  table the project marked operation-traceable, or a configured section), so
  no project has to restructure its documents to be audited;
* a table is acceptance-bearing when one of its COLUMNS is an acceptance
  column. The built-in header vocabulary is natural-language (English +
  Japanese), extended — never replaced — by
  ``acceptance_evidence.acceptance_columns`` in codd.yaml, mirroring the
  ``out_of_scope_markers`` precedent;
* the machine-readable declarations (``verified_by``, ``params``,
  ``critical``) are OPTIONAL EXTRA COLUMNS of the existing table. A project
  adopts them incrementally; a project that has not adopted them is still
  audited through the anchors it already has (``operation_flow.<id>``
  references, requirement-id tokens in source, VB coverage markers).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from codd.requirement_reconciliation import (
    _OPERATION_REFERENCE_RE,
    _TABLE_SEPARATOR_RE,
    _clean_label,
    _parse_tables,
    _string_tuple,
    _table_in_scope,
    discover_requirement_docs,
    requirement_reconciliation_settings,
)

SETTINGS_KEY = "acceptance_evidence"

# Header vocabulary. Natural-language only (no framework/project words), and
# ADDITIVE: a project's ``acceptance_columns`` extends these instead of
# replacing them, because a replacement list silently un-audits every table
# whose header the project forgot to re-list.
DEFAULT_ACCEPTANCE_COLUMNS: tuple[str, ...] = (
    "acceptance",
    "acceptance criteria",
    "acceptance criterion",
    "acceptance_criteria",
    "definition of done",
    "dod",
    "受入条件",
    "受け入れ条件",
    "受入れ条件",
    "検収条件",
    "合格条件",
    "完了条件",
    "完了を確認する状態",
)
VERIFIED_BY_COLUMNS: tuple[str, ...] = (
    "verified_by",
    "verified by",
    "verifiedby",
    "evidence",
    "検証手段",
    "証拠",
)
PARAMS_COLUMNS: tuple[str, ...] = (
    "params",
    "parameters",
    "パラメータ",
    "パラメーター",
)
CRITICAL_COLUMNS: tuple[str, ...] = (
    "critical",
    "重要",
    "重要度",
)

# ``verified_by`` grammar: ``<kind>:<target>``, whitespace/comma/``<br>``
# separated. The kinds are the three evidence classes of invariant (a).
EVIDENCE_KINDS: tuple[str, ...] = ("test", "runtime", "manual")
_EVIDENCE_TOKEN_RE = re.compile(
    r"\b(?P<kind>test|runtime|manual)\s*[:=]\s*(?P<target>[^\s,;|]+)",
    re.IGNORECASE,
)

# ``params`` grammar: ``name=value`` / ``name: value`` pairs, optionally inside
# ``{...}``. Names are identifier-shaped so a prose cell cannot masquerade as a
# declaration.
_PARAM_PAIR_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?P<value>[^,;}\s|]+)",
)

_TRUTHY = frozenset({"true", "yes", "y", "1", "critical", "必須", "はい"})

# A "declared" cell that means "nothing here". Treated as an empty cell so a
# placeholder dash is never parsed as an acceptance criterion.
_EMPTY_CELL_VALUES = frozenset({"", "-", "—", "–", "ー", "n/a", "na", "なし", "未定"})

# A requirement-unit id: the first cell of a requirement table row. Bounded so a
# prose first cell (a paragraph, a date, a sentence) is not mistaken for an id.
_REQUIREMENT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*$")

_SEPARATOR_ROW_RE = re.compile(r"^[\s|:\-]+$")


@dataclass(frozen=True)
class EvidenceRef:
    """One declared evidence pointer, e.g. ``test:qrSheet`` / ``runtime:print``."""

    kind: str
    target: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.kind}:{self.target}"


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One acceptance criterion: a requirement table row's acceptance cell."""

    req_id: str
    source: str
    section: str
    text: str
    row_text: str
    # Declared (optional) machine-readable columns.
    verified_by: tuple[EvidenceRef, ...] = ()
    params: Mapping[str, str] = field(default_factory=dict)
    critical: bool = False
    declares_verified_by: bool = False
    declares_params: bool = False
    # ``operation_flow.<id>`` references found anywhere in the row.
    operation_refs: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.req_id} ({self.source})"

    def evidence_of(self, kind: str) -> tuple[EvidenceRef, ...]:
        return tuple(ref for ref in self.verified_by if ref.kind == kind)


@dataclass(frozen=True)
class AcceptanceSettings:
    """``acceptance_evidence:`` section of codd.yaml."""

    enabled: bool = True
    docs: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    acceptance_columns: tuple[str, ...] = ()
    runtime_severity: str = "red"
    unbound_severity: str = "red"
    param_severity: str = "red"
    undeclared_numeric_severity: str = "amber"
    reachability_severity: str = "amber"
    freshness_severity: str = "red"
    max_findings: int = 30

    @property
    def effective_acceptance_columns(self) -> tuple[str, ...]:
        return DEFAULT_ACCEPTANCE_COLUMNS + self.acceptance_columns


def acceptance_settings(config: Mapping[str, Any] | None) -> AcceptanceSettings:
    """Resolve :class:`AcceptanceSettings` from a project config mapping."""

    section = config.get(SETTINGS_KEY) if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        section = {}

    # ``docs``/``sections`` default to the requirement_reconciliation values so a
    # project that already told CoDD where its requirements live does not have to
    # say it twice.
    reconciliation = requirement_reconciliation_settings(config or {})
    docs = _string_tuple(section.get("docs")) or reconciliation.docs
    sections = _string_tuple(section.get("sections")) or reconciliation.sections

    def severity(key: str, default: str) -> str:
        value = str(section.get(key, default) or default).strip().lower()
        return value if value in {"red", "amber"} else default

    raw_max = section.get("max_findings")
    try:
        max_findings = int(raw_max) if raw_max is not None else 30
    except (TypeError, ValueError):
        max_findings = 30

    return AcceptanceSettings(
        enabled=bool(section.get("enabled", True)),
        docs=docs,
        sections=sections,
        acceptance_columns=_string_tuple(section.get("acceptance_columns")),
        runtime_severity=severity("runtime_severity", "red"),
        unbound_severity=severity("unbound_severity", "red"),
        param_severity=severity("param_severity", "red"),
        undeclared_numeric_severity=severity("undeclared_numeric_severity", "amber"),
        reachability_severity=severity("reachability_severity", "amber"),
        freshness_severity=severity("freshness_severity", "red"),
        max_findings=max(1, max_findings),
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_acceptance_criteria(
    text: str,
    source: str,
    *,
    sections: tuple[str, ...] = (),
    acceptance_columns: tuple[str, ...] = DEFAULT_ACCEPTANCE_COLUMNS,
) -> list[AcceptanceCriterion]:
    """Extract acceptance criteria (one per requirement table row) from Markdown.

    A table contributes criteria only when BOTH hold:

    1. it is in scope for requirement auditing (the shared
       ``requirement_reconciliation`` rule: the table already references an
       ``operation_flow.<id>``, or its section heading is configured), and
    2. one of its header cells is an acceptance column.

    Rule 2 is what keeps this from inventing acceptance criteria: a table that
    lists requirements without stating how they are accepted declares no AC,
    and is silent here rather than flooding with empty obligations.
    """

    criteria: list[AcceptanceCriterion] = []
    for table in _parse_tables(text):
        if not _table_in_scope(table, sections):
            continue
        rows = list(table.rows)
        if len(rows) < 3 or not _TABLE_SEPARATOR_RE.match(rows[1].strip().strip("|")):
            continue
        header = _positional_cells(rows[0])
        columns = _map_columns(header, acceptance_columns)
        acceptance_index = columns.get("acceptance")
        if acceptance_index is None:
            continue
        for row in rows[2:]:
            if _SEPARATOR_ROW_RE.match(row.strip()):
                continue
            cells = _positional_cells(row)
            if len(cells) <= acceptance_index:
                continue
            req_id = _clean_label(cells[0])
            if not _REQUIREMENT_ID_RE.match(req_id):
                continue
            acceptance_text = cells[acceptance_index]
            if _is_empty_cell(acceptance_text):
                continue
            row_text = " ".join(cell for cell in cells if cell)
            verified_by, declares_verified_by = _column_evidence(cells, columns.get("verified_by"))
            params, declares_params = _column_params(cells, columns.get("params"))
            criteria.append(
                AcceptanceCriterion(
                    req_id=req_id,
                    source=source,
                    section=table.section,
                    text=acceptance_text,
                    row_text=row_text,
                    verified_by=verified_by,
                    params=params,
                    critical=_column_critical(cells, columns.get("critical")),
                    declares_verified_by=declares_verified_by,
                    declares_params=declares_params,
                    operation_refs=tuple(
                        sorted({match.group(1) for match in _OPERATION_REFERENCE_RE.finditer(row_text)})
                    ),
                )
            )
    return criteria


def load_acceptance_criteria(
    project_root: Path | str,
    config: Mapping[str, Any] | None = None,
) -> list[AcceptanceCriterion]:
    """Load every acceptance criterion declared by the project's requirement docs."""

    root = Path(project_root).resolve()
    config = config or {}
    settings = acceptance_settings(config)
    if not settings.enabled:
        return []

    # Honor ``acceptance_evidence.docs`` when it differs from the reconciliation
    # docs by handing discover_requirement_docs an overlay config.
    discovery_config: dict[str, Any] = dict(config)
    if settings.docs:
        overlay = dict(discovery_config.get("requirement_reconciliation") or {})
        overlay["docs"] = list(settings.docs)
        discovery_config["requirement_reconciliation"] = overlay

    criteria: list[AcceptanceCriterion] = []
    for doc_path in discover_requirement_docs(root, discovery_config):
        try:
            text = doc_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        source = doc_path.relative_to(root).as_posix() if doc_path.is_relative_to(root) else str(doc_path)
        criteria.extend(
            parse_acceptance_criteria(
                text,
                source,
                sections=settings.sections,
                acceptance_columns=settings.effective_acceptance_columns,
            )
        )
    return criteria


def project_declares_acceptance_criteria(
    project_root: Path | str,
    config: Mapping[str, Any] | None = None,
) -> bool:
    """Whether this project writes acceptance criteria in its requirement docs.

    The structural signal that a project has SOMETHING to certify — used by the
    VB-registry requirement so the "no VB table" hard-fail reaches brownfield
    projects (which never ran ``codd generate`` and therefore own no registry)
    without gating a project that declares nothing at all.
    """

    return bool(load_acceptance_criteria(project_root, config))


# ---------------------------------------------------------------------------
# Invariant (a): runtime obligations must be executable
# ---------------------------------------------------------------------------


def runtime_smoke_enabled(config: Mapping[str, Any] | None) -> bool:
    """Whether the project's runtime smoke stage is configured to run.

    A commented-out / absent / ``enabled: false`` ``runtime_smoke`` section means
    Step 8 never executes. That is the DEMOTION this module refuses to leave
    silent: an acceptance criterion whose only evidence is a runtime behaviour,
    in a project whose runtime stage cannot run, has no evidence at all.
    """

    section = config.get("runtime_smoke") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        return False
    return bool(section.get("enabled", False))


def runtime_obligations(
    criteria: Iterable[AcceptanceCriterion],
    declared_operation_ids: frozenset[str],
) -> list[tuple[AcceptanceCriterion, tuple[str, ...]]]:
    """Acceptance criteria that can only be honoured by running the system.

    Two sources, in priority order:

    1. an EXPLICIT ``verified_by: runtime:<case>`` declaration;
    2. for a project that has not adopted the ``verified_by`` column, an
       ``operation_flow.<id>`` reference that RESOLVES to a declared operation.
       CoDD already treats ``operation_flow`` records as the authoritative
       source of operational (runtime) test obligations — an acceptance
       criterion anchored to one is therefore declaring runtime evidence, in
       the project's own existing vocabulary.

    A dangling reference is deliberately NOT an obligation here: that is
    ``requirement_reconciliation``'s dangling-reference finding, and counting it
    twice would double-report one defect.
    """

    obligations: list[tuple[AcceptanceCriterion, tuple[str, ...]]] = []
    for criterion in criteria:
        explicit = criterion.evidence_of("runtime")
        if explicit:
            obligations.append((criterion, tuple(ref.target for ref in explicit)))
            continue
        if criterion.declares_verified_by:
            # The project adopted the column and did NOT declare runtime
            # evidence for this AC — respect the declaration.
            continue
        resolving = tuple(ref for ref in criterion.operation_refs if ref.lower() in declared_operation_ids)
        if resolving:
            obligations.append((criterion, resolving))
    return obligations


# ---------------------------------------------------------------------------
# Anchors: which files claim to implement / verify a requirement
# ---------------------------------------------------------------------------

# A requirement id written into a file (``QR sheet (F-E2 / design C-1)``) is an
# explicit claim by whoever wrote it: "this file is here because of F-E2". The
# boundary look-arounds keep ``F-E2`` from matching inside ``F-E21`` or
# ``PREF-E2X`` — an id must appear as a whole token.
_ID_BOUNDARY = r"[A-Za-z0-9_.\-]"

# Bound each read so one large artifact cannot stall verification.
_MAX_ANCHOR_SCAN_BYTES = 512 * 1024


def requirement_anchor_pattern(req_ids: Iterable[str]) -> re.Pattern[str] | None:
    """A single alternation matching any of ``req_ids`` as a whole token."""

    ids = sorted({req_id for req_id in req_ids if req_id}, key=len, reverse=True)
    if not ids:
        return None
    body = "|".join(re.escape(req_id) for req_id in ids)
    return re.compile(rf"(?<!{_ID_BOUNDARY})(?P<id>{body})(?!{_ID_BOUNDARY})")


def scan_requirement_anchors(
    project_root: Path | str,
    relative_paths: Iterable[str],
    req_ids: Iterable[str],
) -> dict[str, set[str]]:
    """Map each requirement id to the files that write its id as a token.

    One pass over the candidate files with one combined pattern; unreadable or
    over-sized files are skipped (they anchor nothing rather than raising).
    """

    pattern = requirement_anchor_pattern(req_ids)
    anchors: dict[str, set[str]] = {}
    if pattern is None:
        return anchors
    root = Path(project_root).resolve()
    for relative in relative_paths:
        path = root / relative
        try:
            if path.stat().st_size > _MAX_ANCHOR_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in pattern.finditer(text):
            anchors.setdefault(match.group("id"), set()).add(relative)
    return anchors


def resolve_test_targets(
    target: str,
    test_paths: Iterable[str],
) -> list[str]:
    """Resolve a ``verified_by: test:<target>`` pointer to test file paths.

    ``<target>`` matches a path, a filename, or a filename stem (so both
    ``test:tests/unit/qrSheet.test.ts`` and ``test:qrSheet`` resolve). Matching
    is case-insensitive because filename case conventions differ per language.
    """

    needle = target.strip().strip("`").replace("\\", "/").casefold()
    if not needle:
        return []
    matches: list[str] = []
    for relative in test_paths:
        normalized = relative.replace("\\", "/").casefold()
        name = normalized.rsplit("/", 1)[-1]
        stem = name.split(".", 1)[0]
        if needle in (normalized, name, stem) or normalized.endswith("/" + needle):
            matches.append(relative)
    return sorted(matches)


@dataclass(frozen=True)
class EvidenceContext:
    """Everything the gate and the ``codd acceptance`` commands both need.

    Assembled once from the DAG (whose node ids ARE the project-relative file
    paths) so the check and the CLI cannot drift into two different notions of
    "the tests" or "the implementers of R".
    """

    criteria: tuple[AcceptanceCriterion, ...]
    test_paths: frozenset[str]
    impl_paths: frozenset[str]
    anchors: Mapping[str, frozenset[str]]

    def implementers(self, req_id: str) -> tuple[str, ...]:
        return tuple(sorted(self.anchors.get(req_id, frozenset()) & self.impl_paths))

    def anchored_tests(self, req_id: str) -> tuple[str, ...]:
        return tuple(sorted(self.anchors.get(req_id, frozenset()) & self.test_paths))


def build_evidence_context(
    project_root: Path | str,
    config: Mapping[str, Any] | None = None,
    dag: Any | None = None,
) -> EvidenceContext:
    """Load acceptance criteria plus the file anchors that claim them."""

    root = Path(project_root).resolve()
    config = config or {}
    criteria = load_acceptance_criteria(root, config)
    if dag is None:
        from codd.dag.builder import build_dag

        dag = build_dag(root)
    nodes = getattr(dag, "nodes", {})
    test_paths = frozenset(node.id for node in nodes.values() if getattr(node, "kind", "") == "test_file")
    impl_paths = frozenset(
        node.id for node in nodes.values() if getattr(node, "kind", "") in {"impl_file", "common"}
    )
    anchors = scan_requirement_anchors(
        root,
        sorted(test_paths | impl_paths),
        (criterion.req_id for criterion in criteria),
    )
    return EvidenceContext(
        criteria=tuple(criteria),
        test_paths=test_paths,
        impl_paths=impl_paths,
        anchors={req_id: frozenset(paths) for req_id, paths in anchors.items()},
    )


def vb_id_for(req_id: str) -> str:
    """Canonical VB id derived from a requirement id (``F-E2`` -> ``VB-F-E2``).

    Deterministic and reversible by eye, so a generated registry row is
    obviously about the criterion it came from.
    """

    body = re.sub(r"[^A-Za-z0-9_.-]", "-", req_id.strip()).strip("-")
    return f"VB-{body}" if body else ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _positional_cells(row: str) -> list[str]:
    """Split a Markdown table row into cells WITHOUT dropping empty ones.

    Column identity is positional, so ``requirement_reconciliation._row_cells``
    (which filters empties out for lexical matching) cannot be reused here: one
    blank cell would shift every later column by one and bind the wrong header.
    """

    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _normalize_header(cell: str) -> str:
    cleaned = re.sub(r"[*_`]+", "", cell).strip().casefold()
    return re.sub(r"\s+", " ", cleaned)


def _map_columns(header: list[str], acceptance_columns: tuple[str, ...]) -> dict[str, int]:
    """Map logical column names to positional indices in ``header``."""

    mapping: dict[str, int] = {}
    groups = (
        ("acceptance", acceptance_columns),
        ("verified_by", VERIFIED_BY_COLUMNS),
        ("params", PARAMS_COLUMNS),
        ("critical", CRITICAL_COLUMNS),
    )
    for index, cell in enumerate(header):
        normalized = _normalize_header(cell)
        if not normalized:
            continue
        for key, vocabulary in groups:
            if key in mapping:
                continue
            if any(_normalize_header(word) == normalized for word in vocabulary):
                mapping[key] = index
    return mapping


def _is_empty_cell(value: str) -> bool:
    cleaned = re.sub(r"[*_`\s]+", "", value).strip().casefold()
    return cleaned in _EMPTY_CELL_VALUES


def _column_evidence(cells: list[str], index: int | None) -> tuple[tuple[EvidenceRef, ...], bool]:
    if index is None or index >= len(cells):
        return (), False
    raw = cells[index]
    if _is_empty_cell(raw):
        # The COLUMN exists (the project adopted the declaration) but this row
        # left it blank — that is an undeclared AC, not an unadopted project.
        return (), True
    refs = tuple(
        EvidenceRef(kind=match.group("kind").lower(), target=match.group("target").strip())
        for match in _EVIDENCE_TOKEN_RE.finditer(raw)
    )
    return refs, True


def _column_params(cells: list[str], index: int | None) -> tuple[dict[str, str], bool]:
    if index is None or index >= len(cells):
        return {}, False
    raw = cells[index]
    if _is_empty_cell(raw):
        return {}, True
    params = {
        match.group("name"): match.group("value").strip()
        for match in _PARAM_PAIR_RE.finditer(raw)
    }
    return params, True


def _column_critical(cells: list[str], index: int | None) -> bool:
    if index is None or index >= len(cells):
        return False
    cleaned = re.sub(r"[*_`\s]+", "", cells[index]).strip().casefold()
    return cleaned in _TRUTHY
