"""Restoration coverage / limits report — how far brownfield restoration got.

This is the capstone read-only aggregator for CoDD's brownfield reverse-
restoration. R1 turned Infrastructure-as-Code into NFR candidates; R2 made
``codd restore`` emit, per restored document, a machine-readable
``codd_restoration:`` frontmatter block (lifted under ``codd:``) carrying
provenance, confidence bands, open-questions, and assumptions. This module reads
those restored documents back and answers, deterministically and without any
LLM call, the question the user actually cares about:

    "How far can restoration recover the design/requirements from source + IaC
     (the ceiling), and what is the irreducible residue that a human must
     answer rather than have CoDD fabricate?"

It produces a structured :class:`RestorationReport` with four faces:

* **Recovered** — total restored artifacts; provenance-backed statements split
  by confidence band (green vs amber); and per-evidence-source attribution
  (source-code / tests / IaC / rationale-docs), inferred from each provenance
  locator's *shape* (``file::Kind::name`` ⇒ IaC, ``file::test_name`` ⇒ tests,
  a README/ADR/CHANGELOG path ⇒ rationale, any other source path ⇒ source-code).
* **Irrecoverable-in-principle (the ceiling)** — every ``open_question`` across
  all restored artifacts, grouped by theme (rationale/intent, rejected
  alternatives, NFR priority weights, threshold justification, unbuilt intent),
  each flagged ``needs_human_confirmation``. This is the explicit "cannot be
  reverse-derived" list.
* **Coverage by artifact type / V-model layer** — which catalog artifacts were
  restored vs. expected-but-missing, cross-referenced against the project's
  capability profile (e.g. did a long-running-service project recover its
  infrastructure / operations / NFR artifacts?).
* **Maintenance-loop readiness** — whether the restored doc set is a first-class
  maintainable DAG (each restored doc has ``node_id``, ``source: extracted``,
  resolvable ``depends_on``, and is discoverable by the same scanner the DAG
  engines use), with a one-line handoff.

Pure: the only I/O is reading the already-written restored documents (reusing
:func:`codd.scanner._extract_frontmatter` — the same frontmatter parser the DAG
scanner uses, not a reinvention). Degrades gracefully: a project with no
restoration metadata yields a well-formed "nothing restored yet" report.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from codd.confidence import BAND_AMBER, BAND_GREEN
from codd.scanner import _extract_frontmatter

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
# Band names are owned by codd.confidence (the canonical confidence model);
# restored frontmatter uses the green/amber subset (the restore prompt contract
# never emits gray), so only those two are counted here.
BANDS: tuple[str, ...] = (BAND_GREEN, BAND_AMBER)

# Evidence-source classes, inferred from a provenance locator's *shape*.
SRC_IAC = "iac"
SRC_TESTS = "tests"
SRC_RATIONALE = "rationale"
SRC_SOURCE = "source"
SRC_UNKNOWN = "unknown"
EVIDENCE_SOURCES: tuple[str, ...] = (SRC_SOURCE, SRC_TESTS, SRC_IAC, SRC_RATIONALE, SRC_UNKNOWN)

# Open-question themes (the ceiling categories). Each maps to keyword cues found
# in the question / why_unrecoverable text. General, vendor/domain-neutral.
THEME_RATIONALE = "rationale/intent"
THEME_REJECTED = "rejected alternatives"
THEME_NFR_PRIORITY = "NFR priority weights"
THEME_THRESHOLD = "threshold justification"
THEME_UNBUILT = "unbuilt intent"
THEME_OTHER = "other"

OPEN_QUESTION_THEMES: tuple[str, ...] = (
    THEME_RATIONALE,
    THEME_REJECTED,
    THEME_NFR_PRIORITY,
    THEME_THRESHOLD,
    THEME_UNBUILT,
    THEME_OTHER,
)

# Theme → ordered keyword cues. First matching theme wins (most specific first),
# so e.g. "rejected"/"alternative" classify before generic "rationale".
_THEME_CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (THEME_REJECTED, ("rejected", "alternative", "trade-off", "tradeoff", "considered", "instead of", "discarded")),
    (THEME_NFR_PRIORITY, ("priority", "priorit", "weight", "relative importance", "which nfr", "trade off between")),
    (THEME_THRESHOLD, ("threshold", "limit value", "why this value", "magic number", "chosen value", "tuning", "specific number", "timeout value", "retention period")),
    (THEME_UNBUILT, ("planned", "unbuilt", "never implemented", "future", "roadmap", "not yet", "intended but", "deferred")),
    (THEME_RATIONALE, ("rationale", "why", "intent", "business", "motivation", "reason", "decision", "stakeholder", "purpose")),
)

# Catalog artifact ids that are the load-bearing brownfield "ceiling" artifacts
# (infra/ops/NFR). Used to map coverage to V-model layers and to flag the
# capability-conditional ones.
_INFRA_OPS_NFR_ARTIFACT_IDS: tuple[str, ...] = (
    "non_functional_requirements",
    "infrastructure_design",
    "deployment_design",
    "operations_runbook",
)

# Path tokens that classify a restored doc into a catalog artifact id when the
# doc carries no explicit catalog mapping. Mirrors the catalog default_path_globs
# intent without re-importing the glob machinery.
_ARTIFACT_PATH_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("non_functional_requirements", ("non_functional", "/nfr", "nfr_", "nfr.")),
    ("infrastructure_design", ("infra", "infrastructure")),
    ("deployment_design", ("deploy", "deployment")),
    ("operations_runbook", ("operations", "/ops", "runbook")),
    ("requirements", ("requirement",)),
    ("test_doc", ("/test", "test_", "tests/")),
    ("design_spec", ("design", "detailed_design")),
)


# ---------------------------------------------------------------------------
# Locator-shape classifier (evidence-source attribution)
# ---------------------------------------------------------------------------
_RATIONALE_NAME_TOKENS: tuple[str, ...] = (
    "readme",
    "changelog",
    "/adr",
    "adr/",
    "/adr-",
    "adr-",
    "/decisions/",
    "decision-record",
    "docs/decisions",
)

_IAC_KINDS: frozenset[str] = frozenset(
    {
        # kubernetes
        "deployment",
        "statefulset",
        "daemonset",
        "job",
        "cronjob",
        "horizontalpodautoscaler",
        "networkpolicy",
        "poddisruptionbudget",
        "persistentvolumeclaim",
        "service",
        "ingress",
        "configmap",
        "secret",
        # generic IaC marker
        "resource",
    }
)

_IAC_FILE_TOKENS: tuple[str, ...] = (
    ".tf",
    ".tf::",
    "dockerfile",
    "docker-compose",
    "compose.y",
    ".github/workflows",
    "k8s",
    "kubernetes",
    "helm",
    "/manifests/",
    "namespaces",
    "environments",
)


def classify_locator(locator: str) -> str:
    """Classify a single provenance locator into an evidence-source class.

    The classification is purely shape-based, matching how R2 wrote the locators:

    * IaC          — ``file::Kind::name`` where ``Kind`` is a known IaC resource
      kind (Deployment, HorizontalPodAutoscaler, ``resource`` …), or the file is
      an IaC file (``*.tf``, ``Dockerfile``, ``docker-compose*``,
      ``.github/workflows/*``), or a synthetic IaC source
      (``kubernetes::namespaces``, ``github-actions::environments``).
    * tests        — ``file::test_name`` where the trailing segment looks like a
      test function (``test_*`` / ``*_test`` / ``Test*`` / ``it_*`` / ``should_*``).
    * rationale    — a README / ADR / decision-record / CHANGELOG path.
    * source       — any other concrete source path.
    * unknown      — empty / unrecognizable.
    """

    if not isinstance(locator, str):
        return SRC_UNKNOWN
    raw = locator.strip()
    if not raw:
        return SRC_UNKNOWN

    lowered = raw.lower()

    # Rationale docs win first: a README/ADR/CHANGELOG path is unambiguous.
    if any(token in lowered for token in _RATIONALE_NAME_TOKENS):
        return SRC_RATIONALE

    segments = raw.split("::")
    file_part = segments[0]
    file_lower = file_part.lower()

    # IaC by synthetic cross-resource source or by known IaC file token.
    if any(token in lowered for token in _IAC_FILE_TOKENS):
        # A workflow file with a trailing test-like name is still IaC evidence
        # (the CI pipeline), not a unit test — the file token dominates.
        return SRC_IAC

    # IaC by resource-kind shape: file::Kind::name (3 segments) or file::Kind.
    if len(segments) >= 2:
        kind = segments[1].strip().lower()
        if kind in _IAC_KINDS:
            return SRC_IAC

    # Tests by trailing test-name shape: file::test_name.
    if len(segments) >= 2:
        trailing = segments[-1].strip()
        if _looks_like_test_name(trailing):
            return SRC_TESTS

    # A bare test name with no file prefix (R2 falls back to this when a test
    # has no file_path).
    if len(segments) == 1 and _looks_like_test_name(raw):
        return SRC_TESTS

    # Anything else that names a real source file path.
    if file_lower:
        return SRC_SOURCE
    return SRC_UNKNOWN


def _looks_like_test_name(name: str) -> bool:
    """True when ``name`` has the shape of a test function/method identifier.

    Recognizes the common conventions R2 surfaces from the extractor:
    ``test_*`` / ``*_test`` / ``Test*`` (xUnit class) / ``it_*`` / ``should_*``.
    A path-like value (contains a separator) is never a test *name*.
    """

    if not name:
        return False
    base = name.strip()
    # Strip a parametrized suffix like test_x[case] for the shape check.
    bracket = base.find("[")
    if bracket != -1:
        base = base[:bracket]
    if not base or "/" in base or "\\" in base or "." in base:
        return False
    lowered = base.lower()
    if lowered in {"test", "tests"}:
        return False
    return (
        lowered.startswith("test_")
        or lowered.endswith("_test")
        or lowered.startswith("it_")
        or lowered.startswith("should_")
        or base.startswith("Test")
    )


# ---------------------------------------------------------------------------
# Theme classifier (open-question grouping)
# ---------------------------------------------------------------------------
def classify_open_question_theme(question: str, why: str = "") -> str:
    """Group an open-question into one of :data:`OPEN_QUESTION_THEMES`.

    Keyword-cue based, most-specific-first. Returns :data:`THEME_OTHER` when no
    cue matches (so an unrecognized residue is still surfaced, never dropped).
    """

    text = f"{question or ''} {why or ''}".lower()
    if not text.strip():
        return THEME_OTHER
    for theme, cues in _THEME_CUES:
        if any(cue in text for cue in cues):
            return theme
    return THEME_OTHER


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------
@dataclass
class RestoredArtifactSummary:
    """Per-document slice of the report."""

    node_id: str
    path: str
    artifact_id: str | None
    statement_count: int = 0
    band_counts: dict[str, int] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    open_question_count: int = 0
    assumption_count: int = 0
    has_node_id: bool = False
    is_extracted: bool = False
    depends_on_ids: list[str] = field(default_factory=list)
    unresolved_depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "path": self.path,
            "artifact_id": self.artifact_id,
            "statement_count": self.statement_count,
            "band_counts": dict(self.band_counts),
            "source_counts": dict(self.source_counts),
            "open_question_count": self.open_question_count,
            "assumption_count": self.assumption_count,
            "has_node_id": self.has_node_id,
            "is_extracted": self.is_extracted,
            "depends_on": list(self.depends_on_ids),
            "unresolved_depends_on": list(self.unresolved_depends_on),
        }


@dataclass
class OpenQuestionGroup:
    theme: str
    questions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"theme": self.theme, "count": len(self.questions), "questions": list(self.questions)}


@dataclass
class ArtifactTypeCoverage:
    """Coverage of a single catalog artifact type / V-model layer."""

    artifact_id: str
    restored: bool
    expected: bool
    restored_paths: list[str] = field(default_factory=list)
    capability_conditional: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "restored": self.restored,
            "expected": self.expected,
            "restored_paths": list(self.restored_paths),
            "capability_conditional": self.capability_conditional,
            "note": self.note,
        }


@dataclass
class MaintenanceReadiness:
    maintenance_ready: bool
    reasons: list[str] = field(default_factory=list)
    restored_node_count: int = 0
    nodes_missing_node_id: list[str] = field(default_factory=list)
    nodes_missing_extracted_marker: list[str] = field(default_factory=list)
    unresolved_dependencies: dict[str, list[str]] = field(default_factory=dict)
    discoverable_by_scanner: bool = False
    handoff: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "maintenance_ready": self.maintenance_ready,
            "reasons": list(self.reasons),
            "restored_node_count": self.restored_node_count,
            "nodes_missing_node_id": list(self.nodes_missing_node_id),
            "nodes_missing_extracted_marker": list(self.nodes_missing_extracted_marker),
            "unresolved_dependencies": {k: list(v) for k, v in self.unresolved_dependencies.items()},
            "discoverable_by_scanner": self.discoverable_by_scanner,
            "handoff": self.handoff,
        }


@dataclass
class RestorationReport:
    """The full structured restoration coverage / limits report."""

    has_restoration: bool
    total_restored_artifacts: int
    total_statements: int
    band_counts: dict[str, int]
    source_counts: dict[str, int]
    open_question_total: int
    open_question_groups: list[OpenQuestionGroup]
    assumption_total: int
    artifact_type_coverage: list[ArtifactTypeCoverage]
    maintenance: MaintenanceReadiness
    artifacts: list[RestoredArtifactSummary]
    # Open questions for which git-history testimony supplied a candidate_answer
    # lead (still needs_human_confirmation; a lead, not an answer).
    open_questions_with_candidate_answers: int = 0
    project_type: str = "generic"
    project_type_reason: str = ""
    summary_line: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_restoration": self.has_restoration,
            "project_type": self.project_type,
            "project_type_reason": self.project_type_reason,
            "recovered": {
                "total_restored_artifacts": self.total_restored_artifacts,
                "total_statements": self.total_statements,
                "band_counts": dict(self.band_counts),
                "source_counts": dict(self.source_counts),
                "assumption_total": self.assumption_total,
            },
            "irrecoverable_in_principle": {
                "open_question_total": self.open_question_total,
                "open_questions_with_candidate_answers": self.open_questions_with_candidate_answers,
                "groups": [g.to_dict() for g in self.open_question_groups],
            },
            "artifact_type_coverage": [c.to_dict() for c in self.artifact_type_coverage],
            "maintenance": self.maintenance.to_dict(),
            "artifacts": [a.to_dict() for a in self.artifacts],
            "summary_line": self.summary_line,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Public API — building the report
# ---------------------------------------------------------------------------
def build_restoration_report(
    project_root: Path,
    config: Mapping[str, Any] | None = None,
) -> RestorationReport:
    """Build the restoration coverage/limits report by scanning restored docs.

    Reads restored design docs from the project's configured ``doc_dirs`` plus the
    canonical ``codd extract`` output dirs, parses each one's ``codd:``
    frontmatter (via the scanner's own parser), and aggregates. Pure aside from
    reading those files; never invokes an LLM.
    """

    config = dict(config) if isinstance(config, Mapping) else _safe_load_config(project_root)

    project_type, project_type_reason, capabilities = _resolve_project_profile(project_root, config)

    documents = _discover_restored_documents(project_root, config)

    artifacts: list[RestoredArtifactSummary] = []
    band_counts: dict[str, int] = {b: 0 for b in BANDS}
    source_counts: dict[str, int] = {s: 0 for s in EVIDENCE_SOURCES}
    open_questions_by_theme: dict[str, list[dict[str, Any]]] = {t: [] for t in OPEN_QUESTION_THEMES}
    total_statements = 0
    open_question_total = 0
    assumption_total = 0
    candidate_answer_total = 0

    node_ids_present = {nid for nid, _ in documents if nid}
    declared_node_ids = _all_known_node_ids(project_root, config, node_ids_present)

    for node_id_hint, doc_path in documents:
        summary, doc_bands, doc_sources, doc_questions = _summarize_document(
            project_root, doc_path, node_id_hint, declared_node_ids
        )
        if summary is None:
            continue
        artifacts.append(summary)
        total_statements += summary.statement_count
        open_question_total += summary.open_question_count
        assumption_total += summary.assumption_count
        for band, count in doc_bands.items():
            band_counts[band] = band_counts.get(band, 0) + count
        for src, count in doc_sources.items():
            source_counts[src] = source_counts.get(src, 0) + count
        for q in doc_questions:
            theme = q.get("_theme", THEME_OTHER)
            if q.get("candidate_answer"):
                candidate_answer_total += 1
            open_questions_by_theme.setdefault(theme, []).append(
                {k: v for k, v in q.items() if k != "_theme"}
            )

    has_restoration = bool(artifacts)

    groups = [
        OpenQuestionGroup(theme=theme, questions=open_questions_by_theme.get(theme, []))
        for theme in OPEN_QUESTION_THEMES
        if open_questions_by_theme.get(theme)
    ]

    coverage = _compute_artifact_type_coverage(artifacts, capabilities)
    maintenance = _assess_maintenance_readiness(artifacts, has_restoration)

    notes: list[str] = []
    if not has_restoration:
        notes.append(
            "No restoration metadata found. Run `codd extract` then `codd restore` "
            "to reconstruct design docs with provenance, confidence bands, and "
            "open-questions, then re-run this report."
        )
    elif total_statements == 0:
        notes.append(
            "Restored documents were found but none carry a codd_restoration "
            "provenance block. Re-run `codd restore` so each restored doc emits "
            "provenance / confidence_bands / open_questions."
        )

    report = RestorationReport(
        has_restoration=has_restoration,
        total_restored_artifacts=len(artifacts),
        total_statements=total_statements,
        band_counts=band_counts,
        source_counts=source_counts,
        open_question_total=open_question_total,
        open_question_groups=groups,
        assumption_total=assumption_total,
        open_questions_with_candidate_answers=candidate_answer_total,
        artifact_type_coverage=coverage,
        maintenance=maintenance,
        artifacts=artifacts,
        project_type=project_type,
        project_type_reason=project_type_reason,
        notes=notes,
    )
    report.summary_line = _build_summary_line(report)
    return report


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _discover_restored_documents(
    project_root: Path, config: Mapping[str, Any]
) -> list[tuple[str | None, Path]]:
    """Find candidate restored documents (doc_dirs + extract output dirs).

    Returns ``(node_id_hint, path)`` pairs, de-duplicated by resolved path. The
    node_id hint is only used as a fallback label; the authoritative node_id is
    re-read from frontmatter.
    """

    seen: set[Path] = set()
    found: list[tuple[str | None, Path]] = []

    scan_dirs: list[Path] = []
    scan_section = config.get("scan") if isinstance(config, Mapping) else None
    if isinstance(scan_section, Mapping):
        for doc_dir in scan_section.get("doc_dirs", []) or []:
            scan_dirs.append(project_root / str(doc_dir))

    # Canonical + legacy extract output dirs (the same SSOT the planner uses).
    try:
        from codd.extract_paths import extracted_doc_search_dirs

        scan_dirs.extend(extracted_doc_search_dirs(project_root))
    except Exception:
        pass

    for base in scan_dirs:
        if not base.exists() or not base.is_dir():
            continue
        for root, _dirs, files in os.walk(base):
            for fname in sorted(files):
                if not fname.endswith(".md"):
                    continue
                full = Path(root) / fname
                resolved = full.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                found.append((None, full))
    found.sort(key=lambda item: item[1].as_posix())
    return found


def _all_known_node_ids(
    project_root: Path, config: Mapping[str, Any], present: set[str]
) -> set[str]:
    """Best-effort set of resolvable node ids for depends_on resolution.

    Includes node ids declared by every doc the DAG scanner can map, so a
    restored doc's depends_on that points at an authored design doc still counts
    as resolvable.
    """

    known: set[str] = set(present)
    try:
        from codd.scanner import build_document_node_path_map

        known.update(build_document_node_path_map(project_root, dict(config)).keys())
    except Exception:
        pass
    return known


# ---------------------------------------------------------------------------
# Per-document aggregation
# ---------------------------------------------------------------------------
def _summarize_document(
    project_root: Path,
    doc_path: Path,
    node_id_hint: str | None,
    known_node_ids: set[str],
) -> tuple[RestoredArtifactSummary | None, dict[str, int], dict[str, int], list[dict[str, Any]]]:
    """Parse one restored doc; return its summary + roll-up contributions.

    Returns ``(None, {}, {}, [])`` for documents that are not restored docs
    (no ``source: extracted`` AND no restoration metadata) so authored design
    docs in the same dir do not pollute the report.
    """

    codd = _extract_frontmatter(doc_path)
    if not isinstance(codd, dict):
        return None, {}, {}, []

    is_extracted = str(codd.get("source") or "").strip().lower() == "extracted"
    provenance = codd.get("provenance")
    open_questions = codd.get("open_questions")
    assumptions = codd.get("assumptions")
    has_restoration_meta = bool(provenance) or bool(open_questions) or bool(assumptions)

    # Only restored docs participate. A doc is "restored" if it is marked
    # source: extracted OR carries restoration metadata.
    if not is_extracted and not has_restoration_meta:
        return None, {}, {}, []

    try:
        rel = doc_path.relative_to(project_root).as_posix()
    except ValueError:
        rel = doc_path.as_posix()

    node_id_raw = codd.get("node_id")
    node_id = str(node_id_raw).strip() if isinstance(node_id_raw, str) and node_id_raw.strip() else (node_id_hint or "")
    has_node_id = bool(node_id)

    depends_on_ids = _extract_depends_on_ids(codd.get("depends_on"))
    unresolved = [dep for dep in depends_on_ids if dep not in known_node_ids]

    band_counts: dict[str, int] = {b: 0 for b in BANDS}
    source_counts: dict[str, int] = {s: 0 for s in EVIDENCE_SOURCES}
    statement_count = 0

    for item in _iter_provenance_items(provenance):
        statement_count += 1
        band = _normalize_band(item.get("band"))
        band_counts[band] = band_counts.get(band, 0) + 1
        for locator in _iter_locators(item.get("evidence")):
            klass = classify_locator(locator)
            source_counts[klass] = source_counts.get(klass, 0) + 1

    questions_out: list[dict[str, Any]] = []
    for q in _iter_open_questions(open_questions):
        theme = classify_open_question_theme(
            str(q.get("question") or ""), str(q.get("why_unrecoverable") or "")
        )
        entry: dict[str, Any] = {
            "question": q.get("question"),
            "why_unrecoverable": q.get("why_unrecoverable"),
            "needs_human_confirmation": bool(q.get("needs_human_confirmation", True)),
            "node_id": node_id or rel,
            "_theme": theme,
        }
        # H2: git-history testimony may have attached a candidate_answer lead
        # (provenance commit:<sha>, corroborated flag). Pass it through —
        # it is a lead for the human, not an answer, so the question still
        # counts as open above.
        candidate = q.get("candidate_answer")
        if isinstance(candidate, dict) and candidate:
            entry["candidate_answer"] = candidate
        questions_out.append(entry)

    assumption_count = len(_iter_assumptions(assumptions))

    summary = RestoredArtifactSummary(
        node_id=node_id or rel,
        path=rel,
        artifact_id=_classify_artifact_id(rel, str(codd.get("type") or ""), node_id=node_id),
        statement_count=statement_count,
        band_counts={k: v for k, v in band_counts.items() if v},
        source_counts={k: v for k, v in source_counts.items() if v},
        open_question_count=len(questions_out),
        assumption_count=assumption_count,
        has_node_id=has_node_id,
        is_extracted=is_extracted,
        depends_on_ids=depends_on_ids,
        unresolved_depends_on=unresolved,
    )
    nonzero_bands = {k: v for k, v in band_counts.items() if v}
    nonzero_sources = {k: v for k, v in source_counts.items() if v}
    return summary, nonzero_bands, nonzero_sources, questions_out


def _iter_provenance_items(provenance: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(provenance, list):
        return []
    return [item for item in provenance if isinstance(item, dict)]


def _iter_locators(evidence: Any) -> Iterable[str]:
    if isinstance(evidence, str):
        return [evidence]
    if isinstance(evidence, list):
        return [str(e) for e in evidence if isinstance(e, (str, int, float)) and str(e).strip()]
    return []


def _iter_open_questions(open_questions: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(open_questions, list):
        return []
    return [q for q in open_questions if isinstance(q, dict)]


def _iter_assumptions(assumptions: Any) -> list[Any]:
    if not isinstance(assumptions, list):
        return []
    return [a for a in assumptions if a]


def _normalize_band(band: Any) -> str:
    text = str(band or "").strip().lower()
    if text in BANDS:
        return text
    # Unknown / missing band ⇒ treat as amber (lower confidence) so we never
    # over-state green.
    return BAND_AMBER


def _extract_depends_on_ids(depends_on: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(depends_on, list):
        return out
    for dep in depends_on:
        if isinstance(dep, dict):
            dep_id = dep.get("id") or dep.get("node_id")
            if isinstance(dep_id, str) and dep_id.strip():
                out.append(dep_id.strip())
        elif isinstance(dep, str) and dep.strip():
            out.append(dep.strip())
    return out


def _classify_artifact_id(rel_path: str, doc_type: str, node_id: str = "") -> str | None:
    """Map a restored doc to a catalog artifact id.

    Resolution order: (1) the cross-space artifact-id resolver
    (:mod:`codd.artifact_ids`) on the doc's declared ``node_id`` — a doc that
    names its own identity (a catalog id, or a required-artifacts id like
    ``design:operations_runbook`` / DAG-style ``design:operations-runbook``)
    is authoritative; (2) path-token inference; (3) the doc ``type`` field
    (the original heuristics, kept as the fallback).
    """

    if node_id:
        try:
            from codd.artifact_ids import resolve_artifact_id

            resolved = resolve_artifact_id(node_id)
        except Exception:
            resolved = None
        if resolved is not None:
            return resolved.id

    lowered = PurePosixPath(rel_path).as_posix().lower()
    for artifact_id, tokens in _ARTIFACT_PATH_TOKENS:
        if any(token in lowered for token in tokens):
            return artifact_id
    dt = (doc_type or "").strip().lower()
    if dt == "requirement":
        return "requirements"
    if dt == "operations":
        return "operations_runbook"
    if dt == "design":
        return "design_spec"
    if dt == "test":
        return "test_doc"
    return None


# ---------------------------------------------------------------------------
# Artifact-type / V-model coverage
# ---------------------------------------------------------------------------
def _compute_artifact_type_coverage(
    artifacts: list[RestoredArtifactSummary],
    capabilities: Any,
) -> list[ArtifactTypeCoverage]:
    """Cross-ref restored artifact ids against the brownfield ceiling set.

    "Expected" for infra/ops/NFR artifacts is conditional on the project actually
    having an operational surface (``long_running_service`` or a non-``none``
    network surface) — a pure library/CLI is NOT marked as missing an operations
    runbook (consistent with R1/R2's capability gating).
    """

    restored_by_id: dict[str, list[str]] = {}
    for art in artifacts:
        if art.artifact_id:
            restored_by_id.setdefault(art.artifact_id, []).append(art.path)

    has_operational_surface = _has_operational_surface(capabilities)

    coverage: list[ArtifactTypeCoverage] = []

    # Core authored-design artifacts always expected when restoring.
    for artifact_id in ("requirements", "design_spec", "test_doc"):
        restored_paths = restored_by_id.get(artifact_id, [])
        coverage.append(
            ArtifactTypeCoverage(
                artifact_id=artifact_id,
                restored=bool(restored_paths),
                expected=True,
                restored_paths=restored_paths,
                capability_conditional=False,
                note="" if restored_paths else "expected core artifact not restored",
            )
        )

    # Infra/ops/NFR ceiling artifacts: expected only with an operational surface.
    for artifact_id in _INFRA_OPS_NFR_ARTIFACT_IDS:
        restored_paths = restored_by_id.get(artifact_id, [])
        expected = has_operational_surface
        if restored_paths:
            note = ""
        elif expected:
            note = (
                "expected for a project with an operational surface "
                "(long_running_service / network surface) but not restored"
            )
        else:
            note = "not expected (no operational surface detected for this project type)"
        coverage.append(
            ArtifactTypeCoverage(
                artifact_id=artifact_id,
                restored=bool(restored_paths),
                expected=expected,
                restored_paths=restored_paths,
                capability_conditional=True,
                note=note,
            )
        )

    # Surface any restored artifact ids not in the standard set (bonus coverage).
    standard = {"requirements", "design_spec", "test_doc", *(_INFRA_OPS_NFR_ARTIFACT_IDS)}
    for artifact_id, paths in sorted(restored_by_id.items()):
        if artifact_id in standard:
            continue
        coverage.append(
            ArtifactTypeCoverage(
                artifact_id=artifact_id,
                restored=True,
                expected=False,
                restored_paths=paths,
                capability_conditional=False,
                note="restored (not part of the standard expected set)",
            )
        )

    return coverage


def _has_operational_surface(capabilities: Any) -> bool:
    if capabilities is None:
        # Untyped/legacy projects historically behave like a web app.
        return True
    long_running = bool(getattr(capabilities, "long_running_service", False))
    network = getattr(capabilities, "network_surface", "none")
    return long_running or (isinstance(network, str) and network not in ("", "none"))


# ---------------------------------------------------------------------------
# Maintenance-loop readiness
# ---------------------------------------------------------------------------
def _assess_maintenance_readiness(
    artifacts: list[RestoredArtifactSummary], has_restoration: bool
) -> MaintenanceReadiness:
    """Decide whether the restored set plugs into the DAG maintenance engines.

    A restored doc is DAG-maintainable when it (a) has a ``node_id``, (b) is
    marked ``source: extracted``, and (c) every ``depends_on`` resolves to a
    known node. Discovery is implied because we found these docs by scanning the
    same ``doc_dirs`` + extract output that ``build_document_node_path_map`` and
    the propagate/verify engines scan.
    """

    if not has_restoration:
        return MaintenanceReadiness(
            maintenance_ready=False,
            reasons=["no restored documents found"],
            restored_node_count=0,
            discoverable_by_scanner=False,
            handoff=(
                "Run `codd extract` + `codd restore` first; restored docs become "
                "maintainable DAG nodes once written."
            ),
        )

    missing_node_id = [a.path for a in artifacts if not a.has_node_id]
    missing_extracted = [a.path for a in artifacts if not a.is_extracted]
    unresolved: dict[str, list[str]] = {
        a.node_id: a.unresolved_depends_on for a in artifacts if a.unresolved_depends_on
    }

    reasons: list[str] = []
    if missing_node_id:
        reasons.append(f"{len(missing_node_id)} restored doc(s) lack a codd.node_id")
    if missing_extracted:
        reasons.append(
            f"{len(missing_extracted)} restored doc(s) lack `source: extracted` "
            "(not recognized as extracted nodes by plan/restore)"
        )
    if unresolved:
        total_unresolved = sum(len(v) for v in unresolved.values())
        reasons.append(
            f"{total_unresolved} unresolved depends_on edge(s) across "
            f"{len(unresolved)} doc(s) (run `codd dag verify` to confirm)"
        )

    ready = not reasons
    if ready:
        reasons.append(
            "all restored docs have node_id + `source: extracted` + resolvable "
            "depends_on; they are first-class maintainable DAG nodes"
        )
        handoff = (
            "Restored docs are maintainable nodes: run `codd dag verify` to "
            "validate the graph, then `codd propagate` / `codd fix` operate on "
            "them like any authored design doc."
        )
    else:
        handoff = (
            "Restored docs are discoverable by the DAG scanner but have gaps "
            "above; fix them (or re-run `codd restore`), then `codd dag verify`."
        )

    return MaintenanceReadiness(
        maintenance_ready=ready,
        reasons=reasons,
        restored_node_count=len(artifacts),
        nodes_missing_node_id=missing_node_id,
        nodes_missing_extracted_marker=missing_extracted,
        unresolved_dependencies=unresolved,
        discoverable_by_scanner=True,
        handoff=handoff,
    )


# ---------------------------------------------------------------------------
# Summary line
# ---------------------------------------------------------------------------
def _build_summary_line(report: RestorationReport) -> str:
    if not report.has_restoration:
        return (
            "No restoration metadata found; run `codd restore` first to "
            "reconstruct design docs with provenance/open-questions."
        )

    total = report.total_statements
    green = report.band_counts.get(BAND_GREEN, 0)
    amber = report.band_counts.get(BAND_AMBER, 0)
    green_pct = round(100 * green / total) if total else 0
    amber_pct = round(100 * amber / total) if total else 0

    present_sources = [s for s in EVIDENCE_SOURCES if report.source_counts.get(s)]
    sources_label = ", ".join(present_sources) if present_sources else "no provenance"

    themes_present = [g.theme for g in report.open_question_groups]
    ceiling_label = ", ".join(themes_present) if themes_present else "none recorded"

    return (
        f"Recovered {total} provenance-backed statement(s) "
        f"({green_pct}% green / {amber_pct}% amber) across "
        f"{report.total_restored_artifacts} restored artifact(s) from "
        f"{{{sources_label}}}; {report.open_question_total} open question(s) "
        f"require human confirmation (irrecoverable in principle: {ceiling_label})."
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_report_json(report: RestorationReport) -> str:
    import json

    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False, sort_keys=False)


def render_report_text(report: RestorationReport) -> str:
    lines: list[str] = []
    lines.append("CoDD Restoration Coverage / Limits Report")
    lines.append("=" * 42)
    lines.append("")
    lines.append(f"Project type: {report.project_type}  ({report.project_type_reason})")
    lines.append("")

    if not report.has_restoration:
        lines.append(report.summary_line)
        for note in report.notes:
            lines.append(f"  - {note}")
        return "\n".join(lines) + "\n"

    # Recovered ------------------------------------------------------------
    green = report.band_counts.get(BAND_GREEN, 0)
    amber = report.band_counts.get(BAND_AMBER, 0)
    lines.append("RECOVERED")
    lines.append("-" * 9)
    lines.append(f"  Restored artifacts:        {report.total_restored_artifacts}")
    lines.append(f"  Provenance-backed statements: {report.total_statements}")
    lines.append(f"    green (high confidence):  {green}")
    lines.append(f"    amber (inferred/single):  {amber}")
    lines.append(f"  Assumptions (need confirmation): {report.assumption_total}")
    lines.append("  Evidence-source attribution:")
    for src in EVIDENCE_SOURCES:
        count = report.source_counts.get(src, 0)
        if count:
            lines.append(f"    {src:<10} {count}")
    lines.append("")

    # Irrecoverable in principle ------------------------------------------
    lines.append("IRRECOVERABLE IN PRINCIPLE (the ceiling)")
    lines.append("-" * 40)
    lines.append(
        f"  {report.open_question_total} open question(s) require human "
        "confirmation, grouped by theme:"
    )
    if report.open_questions_with_candidate_answers:
        lines.append(
            f"  {report.open_questions_with_candidate_answers} of them carry a "
            "candidate_answer lead from git-history testimony (still need human "
            "confirmation)."
        )
    if report.open_question_groups:
        for group in report.open_question_groups:
            lines.append(f"  [{group.theme}] ({len(group.questions)})")
            for q in group.questions:
                question = str(q.get("question") or "").strip()
                why = str(q.get("why_unrecoverable") or "").strip()
                lines.append(f"    - {question}")
                if why:
                    lines.append(f"      why: {why}")
    else:
        lines.append("  (none recorded)")
    lines.append("")

    # Coverage by artifact type / V-model layer ---------------------------
    lines.append("COVERAGE BY ARTIFACT TYPE / V-MODEL LAYER")
    lines.append("-" * 41)
    for cov in report.artifact_type_coverage:
        if cov.restored:
            status = "restored"
        elif cov.expected:
            status = "MISSING (expected)"
        else:
            status = "n/a (not expected)"
        suffix = f" — {cov.note}" if cov.note else ""
        lines.append(f"  {cov.artifact_id:<28} {status}{suffix}")
    lines.append("")

    # Maintenance readiness ----------------------------------------------
    m = report.maintenance
    lines.append("MAINTENANCE-LOOP READINESS")
    lines.append("-" * 26)
    lines.append(f"  maintenance_ready: {str(m.maintenance_ready).lower()}")
    for reason in m.reasons:
        lines.append(f"    - {reason}")
    lines.append(f"  handoff: {m.handoff}")
    lines.append("")

    # Summary -------------------------------------------------------------
    lines.append("SUMMARY")
    lines.append("-" * 7)
    lines.append(f"  {report.summary_line}")
    for note in report.notes:
        lines.append(f"  note: {note}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Config / profile resolution (best-effort, fail-open)
# ---------------------------------------------------------------------------
def _safe_load_config(project_root: Path) -> dict[str, Any]:
    try:
        from codd.config import load_project_config

        cfg = load_project_config(project_root)
        return dict(cfg) if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _resolve_project_profile(
    project_root: Path, config: Mapping[str, Any]
) -> tuple[str, str, Any]:
    """Resolve (project_type, reason, capabilities) with safe fallbacks."""

    configured = ""
    if isinstance(config, Mapping):
        ra = config.get("required_artifacts")
        if isinstance(ra, Mapping):
            configured = str(ra.get("project_type") or "").strip().lower()
        if not configured:
            proj = config.get("project")
            if isinstance(proj, Mapping):
                configured = str(proj.get("type") or "").strip().lower()
        if not configured:
            configured = str(config.get("project_type") or "").strip().lower()

    try:
        from codd.project_types import load_capabilities, resolve_project_type

        resolved, reason = resolve_project_type(configured or None, None, project_root)
        capabilities = load_capabilities(resolved, project_root)
        return resolved, reason, capabilities
    except Exception:
        # Fail open: untyped → web-like operational surface so we don't wrongly
        # declare infra/ops artifacts "not expected".
        return "generic", "could not resolve project type; assuming operational surface", None
