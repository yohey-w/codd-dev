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
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

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


# ``mode`` decides the DEFAULT severity of every finding class, and nothing else.
# Advisory (the default) reports exactly the same findings as strict — it just
# does not fail the build with them. That split exists because the two things a
# new check can get wrong are opposite: fail an existing project on an upgrade
# nobody asked for, or stay so quiet that the hole it was written for is
# invisible again. Advisory refuses both: every finding is still a finding, in
# the report, named, with a remedy — it is simply amber until the project says
# `mode: strict`.
MODE_ADVISORY = "advisory"
MODE_STRICT = "strict"


@dataclass(frozen=True)
class AcceptanceSettings:
    """``acceptance_evidence:`` section of codd.yaml."""

    enabled: bool = True
    mode: str = MODE_ADVISORY
    docs: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    acceptance_columns: tuple[str, ...] = ()
    runtime_severity: str = "amber"
    runtime_execution_severity: str = "amber"
    runtime_max_age_hours: float | None = None
    unbound_severity: str = "amber"
    param_severity: str = "amber"
    undeclared_numeric_severity: str = "amber"
    reachability_severity: str = "amber"
    freshness_severity: str = "amber"
    vacuous_severity: str = "amber"
    max_findings: int = 30

    @property
    def strict(self) -> bool:
        return self.mode == MODE_STRICT

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

    mode = str(section.get("mode", MODE_ADVISORY) or MODE_ADVISORY).strip().lower()
    if mode not in {MODE_ADVISORY, MODE_STRICT}:
        mode = MODE_ADVISORY

    def severity(key: str, strict_default: str) -> str:
        # Explicit per-class configuration always wins; `mode` only chooses the
        # default. A project can therefore run advisory overall and still hard-fail
        # the one class it has already cleaned up, or run strict and keep one class
        # amber while it migrates.
        default = strict_default if mode == MODE_STRICT else "amber"
        value = str(section.get(key, default) or default).strip().lower()
        return value if value in {"red", "amber"} else default

    raw_max = section.get("max_findings")
    try:
        max_findings = int(raw_max) if raw_max is not None else 30
    except (TypeError, ValueError):
        max_findings = 30

    raw_max_age = section.get("runtime_max_age_hours")
    try:
        max_age_hours = float(raw_max_age) if raw_max_age is not None else None
    except (TypeError, ValueError):
        max_age_hours = None
    if max_age_hours is not None and max_age_hours <= 0:
        max_age_hours = None

    return AcceptanceSettings(
        enabled=bool(section.get("enabled", True)),
        mode=mode,
        docs=docs,
        sections=sections,
        acceptance_columns=_string_tuple(section.get("acceptance_columns")),
        runtime_severity=severity("runtime_severity", "red"),
        # Amber even under `mode: strict` unless set explicitly: a project that
        # never persisted an execution record is UNPROVEN, not proven wrong, and
        # upgrading CoDD must not turn an existing build red on its own.
        runtime_execution_severity=severity("runtime_execution_severity", "amber"),
        runtime_max_age_hours=max_age_hours,
        unbound_severity=severity("unbound_severity", "red"),
        param_severity=severity("param_severity", "red"),
        undeclared_numeric_severity=severity("undeclared_numeric_severity", "amber"),
        reachability_severity=severity("reachability_severity", "amber"),
        freshness_severity=severity("freshness_severity", "red"),
        vacuous_severity=severity("vacuous_severity", "red"),
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


# Why an enabled runtime stage is not yet evidence, in one word each.
EXECUTION_NO_RECORD = "no_record"
EXECUTION_CONFIG_CHANGED = "config_changed"
EXECUTION_STALE = "stale"

_EXECUTION_REASON_TEXT = {
    EXECUTION_NO_RECORD: (
        "no runtime execution has been recorded (`{ledger}` is absent or unreadable)"
    ),
    EXECUTION_CONFIG_CHANGED: (
        "the recorded run targeted a DIFFERENT runtime configuration — "
        "`runtime_smoke`/`runtime` changed in codd.yaml since `{ledger}` was written"
    ),
    EXECUTION_STALE: "the recorded run is {age:.0f}h old, past `runtime_max_age_hours: {limit:.0f}`",
}


def runtime_execution_evidence(
    project_root: Path | str,
    config: Mapping[str, Any] | None,
    max_age_hours: float | None = None,
    now: datetime | None = None,
) -> tuple[Any | None, str, str]:
    """The recorded runtime execution, or WHY there is none to lean on.

    ``runtime_smoke.enabled: true`` declares the stage; this asks whether it ran.
    Three ways a declaration fails to become evidence, each with its own remedy:
    nothing was ever recorded, the recorded run targeted a configuration the
    project has since changed, or the record is older than the project's own
    freshness window.

    Returns ``(record, reason, detail)`` — ``reason`` is empty exactly when the
    record may be leaned on.
    """

    from codd.runtime_record import (
        ledger_path,
        load_runtime_ledger,
        runtime_config_digest,
    )

    root = Path(project_root)
    ledger = ledger_path(root)
    display = ledger.name
    record = load_runtime_ledger(root)
    if record is None:
        return None, EXECUTION_NO_RECORD, _EXECUTION_REASON_TEXT[EXECUTION_NO_RECORD].format(ledger=display)
    if record.config_digest != runtime_config_digest(config):
        return (
            record,
            EXECUTION_CONFIG_CHANGED,
            _EXECUTION_REASON_TEXT[EXECUTION_CONFIG_CHANGED].format(ledger=display),
        )
    if max_age_hours is not None:
        age = record.age_hours(now)
        if age > max_age_hours:
            return (
                record,
                EXECUTION_STALE,
                _EXECUTION_REASON_TEXT[EXECUTION_STALE].format(age=age, limit=max_age_hours),
            )
    return record, "", ""


def unexecuted_runtime_targets(
    criterion: AcceptanceCriterion,
    targets: Iterable[str],
    record: Any,
) -> tuple[str, ...]:
    """Declared runtime targets that the recorded run did not exercise.

    The same explicit/inferred asymmetry :func:`runtime_obligations` is built on.
    An EXPLICIT ``verified_by: runtime:<case>`` names a case, so a run in which
    no check answers to that name has not discharged it. An INFERRED obligation
    (an ``operation_flow.<id>`` reference in a project that never adopted the
    column) declares no such correspondence, and CoDD does not invent one — any
    recorded run satisfies it.
    """

    if record is None or not criterion.evidence_of("runtime"):
        return ()
    return tuple(target for target in targets if not record.covers(target))


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


def scan_requirement_anchor_hits(
    project_root: Path | str,
    relative_paths: Iterable[str],
    req_ids: Iterable[str],
) -> dict[str, dict[str, tuple[int, ...]]]:
    """Map each requirement id to {file: offsets where its id is written}.

    Offsets matter: in a test file the id is a positioned claim, and the test it
    attaches to is found by position. One pass over the candidate files with one
    combined pattern; unreadable or over-sized files are skipped (they anchor
    nothing rather than raising).
    """

    pattern = requirement_anchor_pattern(req_ids)
    hits: dict[str, dict[str, list[int]]] = {}
    if pattern is None:
        return {}
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
            hits.setdefault(match.group("id"), {}).setdefault(relative, []).append(match.start())
    return {
        req_id: {path: tuple(offsets) for path, offsets in by_path.items()}
        for req_id, by_path in hits.items()
    }


def scan_requirement_anchors(
    project_root: Path | str,
    relative_paths: Iterable[str],
    req_ids: Iterable[str],
) -> dict[str, set[str]]:
    """Map each requirement id to the files that write its id as a token."""

    return {
        req_id: set(by_path)
        for req_id, by_path in scan_requirement_anchor_hits(
            project_root, relative_paths, req_ids
        ).items()
    }


# ---------------------------------------------------------------------------
# Substantiveness: a marker is a claim, a test body is the evidence
# ---------------------------------------------------------------------------

# Comments are stripped before any of this is measured. A file that only TALKS
# about a requirement — "// R-1 is out of scope", "/* R-1 NOT implemented */",
# a bare `codd: covers vb=` line with no test under it — was being counted as
# proof that the requirement works, which is the exact false-green this module
# exists to abolish. A claim in a comment is a claim; only an executed assertion
# is evidence.
# ``#[`` is NOT a comment: it opens a Rust attribute (``#[test]`` / ``#[ignore]``),
# and stripping it would erase the very declaration this module looks for.
_LINE_COMMENT_RE = re.compile(r"(?m)(?:^|\s)(?://|#(?!\[))[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# Test DECLARATIONS across the languages CoDD scans. Deliberately shallow: this
# is a presence test, not a parser.
_TEST_DECLARATION_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_.])(?:test|it|describe|context)\s*(?:\.\s*(?:each|concurrent|only|skip|todo|failing)\s*)?[(`])"
    r"|(?:^|\n)\s*(?:async\s+)?def\s+test\w*\s*\("
    r"|(?:^|\n)\s*func\s+Test\w*\s*\("
    r"|(?:^|\n)\s*@Test\b"
    r"|(?:^|\n)\s*#\[test\]",
)

# SKIPPED declarations. A skipped test reports neither pass nor fail — treating
# it as coverage is the same "green by absence" the verification-coverage rule
# already refuses elsewhere in CoDD.
_SKIPPED_DECLARATION_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_.])(?:test|it|describe|context)\s*\.\s*(?:skip|todo|failing)\s*[(`])"
    r"|(?:(?<![A-Za-z0-9_.])(?:xit|xtest|xdescribe)\s*[(`])"
    r"|(?:@\s*pytest\s*\.\s*mark\s*\.\s*(?:skip|skipif|xfail))"
    r"|(?:@\s*unittest\s*\.\s*(?:skip|expectedFailure))"
    r"|(?:^|\n)\s*#\[ignore\]"
    r"|(?:^|\n)\s*@Disabled\b",
)

# Assertion vocabulary. One hit is enough — the question is whether the file
# asserts anything at all, not how well.
_ASSERTION_RE = re.compile(
    # `assert` is a STATEMENT in several languages (`assert rows == 15`), so it
    # may be followed by whitespace; the call-shaped forms must be followed by a
    # call/member character so a bare word in an identifier does not count.
    r"(?<![A-Za-z0-9_])assert\w*[\s(!]"
    r"|(?<![A-Za-z0-9_])(?:"
    r"expect|should\w*|verify|EXPECT_\w+|ASSERT_\w+|XCTAssert\w*"
    r"|t\.(?:Error|Fatal|Errorf|Fatalf)|require\.\w+|chai|panic!"
    r")\s*[(!.]"
)


def strip_comments(text: str) -> str:
    """Blank out comments, PRESERVING offsets.

    Each comment character becomes a space (newlines kept), so a marker found in
    the raw text still points at the same index in the stripped text. Attribution
    depends on that: a ``codd: covers vb=`` line lives in a comment, and the test
    it belongs to is found by offset.
    """

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if character == "\n" else " " for character in match.group(0))

    return _LINE_COMMENT_RE.sub(blank, _BLOCK_COMMENT_RE.sub(blank, text))


@dataclass(frozen=True)
class TestBlock:
    """One test declaration and the span of source that belongs to it."""

    start: int
    end: int
    skipped: bool
    has_assertion: bool

    @property
    def substantive(self) -> bool:
        return not self.skipped and self.has_assertion

    @property
    def reason(self) -> str:
        if self.skipped:
            return "test_skipped"
        if not self.has_assertion:
            return "no_assertion"
        return ""


def test_blocks(text: str) -> list[TestBlock]:
    """Split a test file into blocks, one per test declaration.

    A block runs from its declaration to the next one (or end of file) — shallow
    on purpose, because this is a presence test, not a parser. That is enough for
    the question being asked: does THIS test, the one the marker is attached to,
    run and assert something?
    """

    code = strip_comments(text)
    starts = [match.start() for match in _TEST_DECLARATION_RE.finditer(code)]
    if not starts:
        return []
    bounds = starts + [len(code)]
    skips = [match.start() for match in _SKIPPED_DECLARATION_RE.finditer(code)]

    blocks: list[TestBlock] = []
    for index, start in enumerate(starts):
        end = bounds[index + 1]
        # A skip marker either coincides with the declaration (``test.skip(``) or
        # precedes it as a decorator (``@pytest.mark.skip``), so it belongs to the
        # first declaration at or after it. The lower bound is EXCLUSIVE of the
        # previous declaration's own offset, or a skipped test would also mark the
        # test that follows it as skipped.
        lower = starts[index - 1] + 1 if index else 0
        skipped = any(lower <= position <= start for position in skips)
        blocks.append(
            TestBlock(
                start=start,
                end=end,
                skipped=skipped,
                has_assertion=bool(_ASSERTION_RE.search(code, start, end)),
            )
        )
    return blocks


def block_at(blocks: Sequence[TestBlock], offset: int) -> TestBlock | None:
    """The test a marker at ``offset`` belongs to: **the one that follows it**.

    A ``codd: covers vb=`` line introduces the test written under it — that is
    the form CoDD's own generated test documents ask for, and it is the only
    reading that cannot be gamed. "Somewhere in this file" was the previous rule,
    and it let one unrelated passing test in the same file certify a marker
    attached to a SKIPPED one.

    A marker with nothing after it falls back to the test it sits inside (the
    last declaration before it), so a marker written at the end of a test body
    still attaches to that test. A marker with no test either side proves
    nothing.
    """

    for block in blocks:
        if block.start >= offset:
            return block
    for block in reversed(list(blocks)):
        if block.start < offset:
            return block
    return None


def substance_at(text: str, offset: int) -> tuple[bool, str]:
    """Whether the test a marker at ``offset`` belongs to actually proves something.

    File-level was not enough: one unrelated real test in the same file made a
    ``covers vb=`` marker over a SKIPPED test read as coverage. The claim has to
    be judged against the test it is attached to, not against the file's best
    test.
    """

    blocks = test_blocks(text)
    block = block_at(blocks, offset)
    if block is None:
        return False, "no_test_body"
    return block.substantive, block.reason


def test_substance(text: str) -> tuple[bool, str]:
    """Whether a file contains at least one test that runs and asserts something.

    File granularity, for a claim that names a FILE (``verified_by: test:<name>``)
    rather than a position. A claim that has a position is judged by
    :func:`substance_at` instead.
    """

    blocks = test_blocks(text)
    if not blocks:
        return False, "no_test_body"
    for block in blocks:
        if block.substantive:
            return True, ""
    return False, blocks[0].reason if len(blocks) == 1 else (
        "test_skipped" if all(block.skipped for block in blocks) else "no_assertion"
    )


def substantive_tests(
    project_root: Path | str,
    relative_paths: Iterable[str],
) -> tuple[set[str], dict[str, str]]:
    """Split candidate test files into (substantive, {path: vacuity reason})."""

    root = Path(project_root).resolve()
    good: set[str] = set()
    vacuous: dict[str, str] = {}
    for relative in relative_paths:
        text = read_test_text(root, relative)
        if text is None:
            vacuous[relative] = "unreadable"
            continue
        substantive, reason = test_substance(text)
        if substantive:
            good.add(relative)
        else:
            vacuous[relative] = reason
    return good, vacuous


def read_test_text(project_root: Path | str, relative: str) -> str | None:
    """Read a project file for substantiveness analysis (None when unreadable)."""

    try:
        return (Path(project_root).resolve() / relative).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def cover_marker_offsets(text: str, vb_id: str) -> list[int]:
    """Offsets of ``codd: covers ... vb=<id>`` markers in ``text``."""

    pattern = re.compile(
        r"codd\s*:\s*covers\b[^\n]*?vb\s*=\s*" + re.escape(vb_id) + r"(?![A-Za-z0-9_.:-])",
        re.IGNORECASE,
    )
    return [match.start() for match in pattern.finditer(text)]


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
    dag: Any = None

    def implementers(self, req_id: str) -> tuple[str, ...]:
        return tuple(sorted(self.anchors.get(req_id, frozenset()) & self.impl_paths))

    def accepted_implementation(self, req_id: str) -> tuple[str, ...]:
        """What a manual verdict on ``req_id`` is about: implementers + their imports.

        The CLI records exactly what the check later re-hashes; if the two
        disagreed, every record would read as stale the moment it was written.
        """

        return tuple(
            implementation_closure(self.dag, self.implementers(req_id), self.impl_paths)
        )

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
        dag=dag,
    )


# Invariant (c): a value an acceptance criterion states must reach its evidence
# through a NAME, not by being retyped. A bare literal in the criterion text is
# therefore worth a nudge — but only a bare one. The look-arounds reject every
# number that is part of a larger token, which is where the false positives live:
# an id (``F-E2``), a paper size (``A4``), a date (``2026-08-22``), a version
# (``v1.7``), a path or a decimal inside one.
_NUMERIC_LITERAL_RE = re.compile(r"(?<![A-Za-z0-9_\-./:])\d+(?:[.,]\d+)*(?![A-Za-z0-9_\-./:])")
_MARKUP_RE = re.compile(r"<[^>]+>")


def numeric_literals(text: str) -> list[str]:
    """Bare numeric literals stated in an acceptance criterion, in order, deduped."""

    stripped = _MARKUP_RE.sub(" ", text)
    seen: list[str] = []
    for match in _NUMERIC_LITERAL_RE.finditer(stripped):
        value = match.group(0)
        if value not in seen:
            seen.append(value)
    return seen


def entry_files_by_operation(
    project_root: Path | str,
    config: Mapping[str, Any] | None,
    operations: Mapping[str, Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    """Map each operation id to the ENTRY files a user's request arrives at.

    The entry point is where the shipped path starts: the route/page/handler the
    user actually touches. CoDD already knows how to derive those — a project's
    ``filesystem_routes`` config drives the same extractor the scanner uses — so
    this reads the project's declarations rather than guessing at a framework.

    An operation may also name its entry explicitly (``entry_file:``); that wins,
    because an explicit declaration should never be second-guessed by inference.
    """

    from codd.parsing.filesystem_routes import FileSystemRouteExtractor
    from codd.requirement_reconciliation import _normalize_route

    root = Path(project_root).resolve()
    route_configs = (config or {}).get("filesystem_routes")
    routes_by_url: dict[str, list[str]] = {}
    if isinstance(route_configs, list) and route_configs:
        info = FileSystemRouteExtractor().extract_routes(root, route_configs)
        for route in info.routes:
            try:
                relative = Path(route["file"]).resolve().relative_to(root).as_posix()
            except (ValueError, OSError):
                continue
            normalized = _normalize_route(str(route.get("url", "")))
            if normalized:
                routes_by_url.setdefault(normalized, []).append(relative)

    entries: dict[str, tuple[str, ...]] = {}
    for operation_id, operation in operations.items():
        explicit = operation.get("entry_file") or operation.get("entry")
        if isinstance(explicit, str) and explicit.strip():
            entries[operation_id] = (explicit.strip(),)
            continue
        found: list[str] = []
        raw_routes = operation.get("route") or operation.get("routes")
        values = raw_routes if isinstance(raw_routes, (list, tuple)) else [raw_routes]
        for value in values:
            if not isinstance(value, str):
                continue
            normalized = _normalize_route(value)
            if normalized:
                found.extend(routes_by_url.get(normalized, ()))
        if found:
            entries[operation_id] = tuple(sorted(set(found)))
    return entries


def import_closure(dag: Any, starts: Iterable[str]) -> set[str]:
    """Files reachable from ``starts`` by following the DAG's ``imports`` edges.

    The DAG's import edges are the same per-language extraction the scanner
    builds the graph from, so this inherits both its reach and its limits — a
    dynamic import, a DI container or reflection produces no edge. That is why
    every finding derived from this closure is amber and why an unfollowable
    entry is reported as unknown rather than treated as "not reachable".
    """

    adjacency: dict[str, set[str]] = {}
    for edge in getattr(dag, "edges", ()) or ():
        if getattr(edge, "kind", "") == "imports":
            adjacency.setdefault(edge.from_id, set()).add(edge.to_id)

    seen: set[str] = set()
    stack = [start for start in starts]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(adjacency.get(current, ()))
    return seen


def implementation_closure(
    dag: Any,
    anchored: Iterable[str],
    impl_paths: Iterable[str],
) -> list[str]:
    """The implementation a manual verdict is really about: anchors + what they import.

    Hashing only the files that write the requirement id lets the behaviour walk
    one file sideways and escape (d) entirely: accept ``make-sheet.ts``, then
    change ``15`` to ``20`` in the ``cfg.ts`` it imports, and the recorded
    verdict still looks current — the exact "15 vs 20" drift the invariant was
    written for. The accepted set is therefore the anchors plus their in-tree
    import closure, intersected with the project's implementation files (the
    closure never leaves the DAG, so third-party code is already out).
    """

    anchors = set(anchored)
    if not anchors:
        return []
    reachable = import_closure(dag, anchors)
    return sorted((reachable & set(impl_paths)) | anchors)


def tested_subjects(dag: Any, test_paths: Iterable[str]) -> dict[str, set[str]]:
    """For each test file, the implementation files the DAG says it exercises."""

    wanted = set(test_paths)
    subjects: dict[str, set[str]] = {}
    for edge in getattr(dag, "edges", ()) or ():
        if getattr(edge, "kind", "") == "tested_by" and edge.to_id in wanted:
            subjects.setdefault(edge.to_id, set()).add(edge.from_id)
    return subjects


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
