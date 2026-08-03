"""Canon integrity ledger — byte-identity of the documents CoDD treats as canon.

WHAT THIS DOES NOT DO (read this first)
---------------------------------------
This is **not an authorization gate.** It does not decide who may change canon,
and it cannot tell a legitimate change from an illegitimate one. Anyone — human
or agent — who edits a canon document and then runs ``codd canon accept`` gets a
green check. That is by design, and there is no configuration that changes it.

What it detects is **unaware change**: a modification nobody decided to make.
A formatter re-aligning a table, an editor's format-on-save, a bulk ``sed``, an
agent writing to the wrong path, a bad merge. Those share one property that makes
them catchable — nobody runs ``accept`` afterwards, because nobody knows anything
happened. The ledger turns "no trigger to notice it" into a red check.

So, honestly:

* **Caught:** silent/unintended edits, formatter damage, wrong-file writes,
  accidental deletion, a stale ledger after a rename.
* **NOT caught:** a deliberate edit followed by ``accept``. If the person or
  agent making the change is the one accepting it, the ledger records that it
  happened; it does not judge whether it should have. The defence there is the
  recorded provenance (below) and code review — not this check.

The one lever against reflexive acceptance is **friction plus a paper trail**:
``accept`` refuses to run without a work reference, prints the actual diff of
what is about to be blessed, and writes that reference into the committed ledger
next to the digest. A meaningless acceptance is therefore still *visible* in
``git log`` / ``git blame`` forever. That is a review aid, not a guarantee.

Why this exists
---------------
A requirement unit is a Markdown **table row** (``requirement_reconciliation.
parse_requirement_units``). A formatter that only re-aligns table pipes changes
no meaning, so review does not catch it and every CoDD check stays green — yet
byte-identity between the human-approved original and the document CoDD
reconciles against is gone. Observed in the field: ``npx prettier --write .``
rewrote **440 lines** of a requirements table, caught only because an unrelated
diff check happened to run.

``codd init`` writing a ``.prettierignore`` (v3.38.0) is the *first* layer: it
keeps one named tool away from the canon. It does not help an existing project,
and it does nothing about dprint, ``markdownlint --fix``, an IDE's format-on-save,
a well-meaning bulk ``sed``, or an agent editing the wrong file. This module is
the *second*, tool-independent layer: record what canon looked like when it was
accepted, and refuse to go green when the bytes no longer match.

Design constraints this file honours
------------------------------------
* **Never auto-update.** A ledger that silently refreshes itself detects nothing.
  Updating is an explicit act (``codd canon accept``).
* **One discovery path.** Scope reuses ``requirement_reconciliation.
  discover_requirement_docs`` (the project's own declaration of which documents
  are requirement canon) and then applies ``scan.exclude``. Re-implementing glob
  semantics is exactly what caused the v3.38.0 F-8 bug.
* **Narrow by default.** The ledger covers *requirement* documents, not every
  Markdown file under ``scan.doc_dirs``. CoDD's own AI stages (``generate``,
  ``propagate``, ``fixup-drift``, ``extract``) legitimately rewrite design/test
  documents; ledgering those would put the tripwire red after every normal run
  and train users to run ``accept`` reflexively — a detector dead by alarm
  fatigue. ``canon.docs`` widens the scope when a project wants more.
* **Addition is not alteration.** A document present in scope but absent from
  the ledger is reported, but amber: adding a file is visible in ``git status``.
  The failure mode CoDD exists to stop is a *silent change to an approved
  document* — that is red.
* **Root-jailed.** Every configured path and every rglob match is confined to the
  project root (rglob follows symlinks), so a symlinked out-of-root file can
  never enter the ledger.
* **Merge-friendly on purpose.** The ledger is LINE-ORIENTED — one document per
  line, sorted by path, no enclosing brackets or trailing-comma structure — so two
  branches that accept *different* documents produce edits to different lines and
  Git merges them without a conflict. A nested JSON object would have made every
  concurrent acceptance a hand-resolved conflict, and a format people dread
  merging is a format people stop committing. Two branches accepting the *same*
  document still conflict, which is correct: that is a real disagreement about
  what the approved bytes are.

The ledger file lives at ``<codd-dir>/canon.lock`` and is meant to be COMMITTED:
the shipped ``<config_dir>/.gitignore`` ignores only ``scan/``, ``reports/`` and
``extracted/``.

Format (``canon.lock``)::

    # codd-canon-lock v2
    # <explanatory header lines, all starting with '#'>

    docs/requirements/a.md<TAB>sha256:<hex><TAB>cmd_042

    docs/requirements/b.md<TAB>sha256:<hex><TAB>cmd_042

Fields per line: the project-relative POSIX path, the digest, and the work
reference supplied to the ``accept`` that recorded this digest. Records are
separated by a blank line — measured, not cosmetic: Git conflicts when the two
sides' changed regions touch, so without the separator two branches accepting
documents whose lines happen to be neighbours still collided.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from codd.config import find_codd_dir
from codd.discovery import matches_exclude_pattern, scan_exclude_patterns
from codd.path_safety import resolve_project_path


__all__ = [
    "CANON_LOCK_NAME",
    "LEDGER_VERSION",
    "DIGEST_ALGORITHM",
    "MIN_ACCEPT_REFERENCE_LENGTH",
    "CanonSettings",
    "CanonStatus",
    "LedgerEntry",
    "canon_settings",
    "canon_lock_path",
    "canon_documents",
    "compute_digests",
    "digest_bytes",
    "load_ledger",
    "ledger_digests",
    "normalize_accept_reference",
    "render_ledger",
    "write_ledger",
    "evaluate_canon",
]


#: Name of the ledger file inside the CoDD config dir (``codd/`` or ``.codd/``).
CANON_LOCK_NAME = "canon.lock"

#: Ledger schema version. v1 was a nested JSON object; v2 is the line-oriented
#: format, adopted so concurrent acceptances of DIFFERENT documents merge without
#: a conflict. An unknown/newer version is reported rather than reinterpreted.
LEDGER_VERSION = 2

#: Marker on the ledger's first line. Parsed for the version; also what makes the
#: file self-identifying to anyone who opens it cold.
LEDGER_MAGIC = "# codd-canon-lock v"

DIGEST_ALGORITHM = "sha256"

#: Shortest accepted work reference. Not security — friction plus a paper trail.
#: An empty or one-character value would make the required argument ceremonial.
MIN_ACCEPT_REFERENCE_LENGTH = 3

#: Header written into every ledger so a human who opens the file cold learns
#: what it is, what it does NOT do, and how to change it — without leaving the
#: file. Constant text (no timestamp, no tool version): an accept that changes
#: nothing must produce no diff.
LEDGER_HEADER = (
    "CoDD canon integrity ledger — COMMIT THIS FILE.",
    "",
    "One line per canon document, sorted by path, blank-line separated so that",
    "two branches accepting DIFFERENT documents merge without a conflict:",
    "  <path>\\t<digest>\\t<work reference of the accept that recorded it>",
    "",
    "`codd dag verify` and the pre-commit hook fail red when a digest no longer",
    "matches the file on disk. The usual cause is a formatter (prettier, dprint,",
    "markdownlint --fix, format-on-save) rewriting a requirements table: a",
    "requirement unit is a table ROW, so re-aligning pipes changes no meaning,",
    "passes review, and still destroys byte-identity with the approved original.",
    "",
    "THIS IS NOT AN AUTHORIZATION GATE. It detects change nobody decided to make.",
    "A deliberate edit followed by `codd canon accept` is green — the work",
    "reference recorded on each line is a review trail, not a permission check.",
    "",
    "Update it only via:  codd canon accept --for <work reference>",
    "and only after reading the diff it prints.",
)

_SETTINGS_KEY = "canon"

#: Files CoDD itself GENERATES into the default canon directory. They are output,
#: not approved requirements, so they must never enter the ledger by discovery:
#: ``codd require --audit`` writes ``coverage_audit_report.md`` and its ``--output``
#: defaults to ``docs/requirements/``, so ledgering it would put the tripwire red
#: after an ordinary audit run — the alarm-fatigue failure the narrow default
#: scope exists to avoid, landing on the very files CoDD tells users to keep.
#: ``coverage_auditor`` already carries the same literal for the same reason (it
#: refuses to feed the report back into its own corpus).
#:
#: This applies to DISCOVERY only. A project that lists such a file explicitly in
#: ``canon.docs`` said it on purpose, and an explicit declaration always wins.
GENERATED_DOC_NAMES = frozenset({"coverage_audit_report.md"})


@dataclass(frozen=True)
class CanonSettings:
    """Resolved ``canon:`` section of ``codd.yaml`` (all fields have safe defaults)."""

    enabled: bool = True
    #: Optional explicit scope (files, directories or ``*.md``-bearing dirs).
    #: When empty, requirement-document discovery decides the scope.
    docs: tuple[str, ...] = ()
    #: Severity for an ALTERED or MISSING canon document. Red by default: a
    #: silent edit to an approved document is the failure this exists to catch.
    severity: str = "red"


@dataclass(frozen=True)
class LedgerEntry:
    """One recorded document: its digest and the accept that put it there."""

    path: str
    digest: str
    #: Work reference passed to the ``accept`` that recorded this digest — the
    #: review trail. Empty only for a ledger written before the field existed.
    accepted_for: str = ""


@dataclass
class CanonStatus:
    """Outcome of comparing the ledger against the documents on disk."""

    enabled: bool = True
    ledger_present: bool = False
    #: Project-relative POSIX path of the ledger, or ``None`` when no CoDD
    #: config dir was found.
    ledger_path: str | None = None
    #: Ledger schema version could not be interpreted (forward-incompatible).
    ledger_unreadable: str | None = None
    #: In the ledger AND on disk, digest differs → RED.
    modified: list[str] = field(default_factory=list)
    #: In the ledger, absent on disk (deleted/renamed) → RED.
    missing: list[str] = field(default_factory=list)
    #: In scope on disk, absent from the ledger → AMBER.
    untracked: list[str] = field(default_factory=list)
    #: In the ledger AND on disk with a matching digest.
    verified: list[str] = field(default_factory=list)

    @property
    def checked_count(self) -> int:
        """Documents actually compared against a recorded digest."""
        return len(self.verified) + len(self.modified) + len(self.missing)

    @property
    def has_drift(self) -> bool:
        """True when an accepted document was altered or removed (the red case)."""
        return bool(self.modified or self.missing)

    @property
    def clean(self) -> bool:
        return not (self.modified or self.missing or self.untracked)


# ═══════════════════════════════════════════════════════════
# Settings / paths
# ═══════════════════════════════════════════════════════════


def canon_settings(config: Mapping[str, Any] | None) -> CanonSettings:
    """Read the ``canon:`` section, tolerating a missing/partial config.

    The pre-commit hook reads a RAW ``codd.yaml`` (not merged with
    ``defaults.yaml``), so every default lives here in code as well as in
    ``defaults.yaml`` — the two must agree.
    """
    section = (config or {}).get(_SETTINGS_KEY) if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        return CanonSettings()
    raw_docs = section.get("docs") or []
    if isinstance(raw_docs, str):
        raw_docs = [raw_docs]
    docs = tuple(
        str(item).strip()
        for item in raw_docs
        if isinstance(item, str) and str(item).strip()
    ) if isinstance(raw_docs, (list, tuple)) else ()
    severity = str(section.get("severity") or "red").strip().lower()
    if severity not in {"red", "amber"}:
        severity = "red"
    return CanonSettings(
        enabled=bool(section.get("enabled", True)),
        docs=docs,
        severity=severity,
    )


def canon_lock_path(project_root: Path | str) -> Path | None:
    """Absolute path of ``<codd-dir>/canon.lock``; ``None`` without a config dir."""
    codd_dir = find_codd_dir(Path(project_root))
    if codd_dir is None:
        return None
    return codd_dir / CANON_LOCK_NAME


# ═══════════════════════════════════════════════════════════
# Scope
# ═══════════════════════════════════════════════════════════


def canon_documents(
    project_root: Path | str,
    config: Mapping[str, Any] | None,
) -> list[Path]:
    """Documents in canon scope, root-jailed, ``scan.exclude``-filtered, sorted.

    Scope precedence:

    1. ``canon.docs`` when set — files and/or directories (a directory
       contributes its ``**/*.md``). This is the widening knob for a project
       whose canon is not only ``docs/requirements/``.
    2. Otherwise the project's requirement documents, via
       ``requirement_reconciliation.discover_requirement_docs`` — which itself
       honours ``requirement_reconciliation.docs`` when the project pinned an
       explicit canonical list, and otherwise defaults to
       ``docs/requirements/**/*.md`` plus the conventional top-level files.

    ``scan.exclude`` is applied in BOTH cases: a document the project declared
    "not CoDD's business" is not canon. (v3.38.0 F-8: two gates walked
    ``doc_dirs`` while ignoring ``scan.exclude`` and contradicted the config.)
    """
    root = Path(project_root)
    settings = canon_settings(config)

    explicit_scope = bool(settings.docs)
    if explicit_scope:
        candidates = _expand_configured_paths(root, settings.docs)
    else:
        # Imported lazily: requirement_reconciliation imports config helpers, and
        # a module-level import here would risk a cycle for hook/CLI callers.
        from codd.requirement_reconciliation import discover_requirement_docs

        candidates = list(discover_requirement_docs(root, config or {}))

    exclude_patterns = scan_exclude_patterns(dict(config or {}))
    selected: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        confined = resolve_project_path(root, candidate)
        if confined is None or not confined.is_file():
            continue
        rel = _relative_posix(root, confined)
        if rel is None or rel in seen:
            continue
        if not explicit_scope and confined.name in GENERATED_DOC_NAMES:
            continue
        if any(matches_exclude_pattern(rel, pattern) for pattern in exclude_patterns):
            continue
        seen.add(rel)
        selected.append(confined)
    return sorted(selected, key=lambda path: _relative_posix(root, path) or str(path))


def _expand_configured_paths(root: Path, raw_paths: tuple[str, ...]) -> list[Path]:
    """Resolve ``canon.docs`` entries; a directory contributes its ``**/*.md``.

    Every entry AND every rglob match is jailed: ``canon.docs`` is
    user-controllable and ``rglob`` follows symlinks, so an in-root ``*.md``
    pointing outside the tree must never become a ledgered document.
    """
    expanded: list[Path] = []
    for raw in raw_paths:
        path = resolve_project_path(root, raw)
        if path is None:
            continue
        if path.is_dir():
            expanded.extend(
                confined
                for md_path in sorted(path.rglob("*.md"))
                if (confined := resolve_project_path(root, md_path)) is not None
            )
        elif path.is_file():
            expanded.append(path)
    return expanded


def _relative_posix(root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


# ═══════════════════════════════════════════════════════════
# Digests
# ═══════════════════════════════════════════════════════════


def digest_bytes(data: bytes) -> str:
    """``sha256:<hex>`` of raw bytes.

    Raw bytes, never decoded text: the point is byte-identity with the approved
    original, so a newline-ending or encoding change must register as a change.
    """
    return f"{DIGEST_ALGORITHM}:{hashlib.sha256(data).hexdigest()}"


def compute_digests(
    project_root: Path | str,
    documents: list[Path] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Map project-relative POSIX path → digest for every in-scope document."""
    root = Path(project_root)
    docs = documents if documents is not None else canon_documents(root, config)
    digests: dict[str, str] = {}
    for doc in docs:
        rel = _relative_posix(root, doc)
        if rel is None:
            continue
        try:
            digests[rel] = digest_bytes(doc.read_bytes())
        except OSError:
            # Unreadable in-scope document: omitting it would silently shrink the
            # ledger. Leave it out of the digest map — evaluate_canon reports it
            # as untracked/missing rather than inventing a digest.
            continue
    return dict(sorted(digests.items()))


# ═══════════════════════════════════════════════════════════
# Ledger I/O
# ═══════════════════════════════════════════════════════════


def normalize_accept_reference(reference: str | None) -> str:
    """Validate the work reference an ``accept`` must carry.

    Raises ``ValueError`` on an empty / too-short / multi-line value. This is
    friction and a paper trail, NOT authorization: it cannot tell a real work
    item from an invented one. What it does buy is that the reference is written
    into the committed ledger next to the digest, so a reflexive acceptance is
    permanently visible in ``git log`` and ``git blame`` for a reviewer to catch.
    Tabs and newlines are rejected because they would break the line format.
    """
    text = (reference or "").strip()
    if not text:
        raise ValueError(
            "a work reference is required — pass --for <reference> naming the task, "
            "issue or journal entry this canon change belongs to. Accepting to make "
            "a check go green, with no work item behind it, is the thing this "
            "argument exists to make visible."
        )
    if "\t" in text or "\n" in text or "\r" in text:
        raise ValueError("the work reference must be a single line without tabs")
    if len(text) < MIN_ACCEPT_REFERENCE_LENGTH:
        raise ValueError(
            f"the work reference must be at least {MIN_ACCEPT_REFERENCE_LENGTH} "
            f"characters (got {text!r})"
        )
    return text


def _parse_ledger_version(first_line: str) -> int | None:
    if not first_line.startswith(LEDGER_MAGIC):
        return None
    try:
        return int(first_line[len(LEDGER_MAGIC):].strip())
    except ValueError:
        return None


def load_ledger(path: Path | str) -> dict[str, Any] | None:
    """Read a ledger file. ``None`` when absent; raises ``ValueError`` when corrupt.

    Returns a payload mapping with ``version`` and ``entries`` (a list of
    :class:`LedgerEntry`). A JSON v1 ledger written by 3.39.0 is still parsed, so
    upgrading does not resurrect the "no ledger" advisory on a project that had
    already adopted the mechanism.
    """
    lock_path = Path(path)
    if not lock_path.is_file():
        return None
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{lock_path.name} is not readable: {exc}") from exc

    stripped = text.lstrip()
    if stripped.startswith("{"):
        return _load_legacy_json_ledger(lock_path, text)

    lines = text.splitlines()
    if not lines:
        raise ValueError(f"{lock_path.name} is empty")
    version = _parse_ledger_version(lines[0])
    if version is None:
        raise ValueError(
            f"{lock_path.name} does not start with the '{LEDGER_MAGIC}<n>' marker"
        )

    entries: list[LedgerEntry] = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            raise ValueError(
                f"{lock_path.name} line {number}: expected tab-separated "
                f"<path>\\t<digest>[\\t<reference>], got {line!r}"
            )
        entries.append(
            LedgerEntry(
                path=fields[0].strip(),
                digest=fields[1].strip(),
                accepted_for=fields[2].strip() if len(fields) > 2 else "",
            )
        )
    return {"version": version, "entries": entries}


def _load_legacy_json_ledger(lock_path: Path, text: str) -> dict[str, Any]:
    """Parse the v1 nested-JSON ledger so an early adopter is not reset to zero."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{lock_path.name} is not readable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{lock_path.name} must contain a JSON object")
    documents = payload.get("documents")
    entries = (
        [
            LedgerEntry(path=str(key), digest=str(value))
            for key, value in sorted(documents.items())
            if isinstance(key, str) and isinstance(value, str)
        ]
        if isinstance(documents, Mapping)
        else []
    )
    return {"version": payload.get("version"), "entries": entries}


def ledger_digests(payload: Mapping[str, Any] | None) -> dict[str, str]:
    """Extract the path → digest mapping from a ledger payload."""
    if not isinstance(payload, Mapping):
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return {}
    return {
        entry.path: entry.digest
        for entry in entries
        if isinstance(entry, LedgerEntry) and entry.path and entry.digest
    }


def render_ledger(entries: Mapping[str, LedgerEntry] | list[LedgerEntry]) -> str:
    """Serialize the ledger: header comments, then one sorted line per document.

    Line-oriented and sorted so Git merges two branches that accepted DIFFERENT
    documents without a conflict; there is no enclosing bracket or trailing-comma
    structure whose last element would churn on every append.
    """
    records = list(entries.values()) if isinstance(entries, Mapping) else list(entries)
    lines = [f"{LEDGER_MAGIC}{LEDGER_VERSION}"]
    lines.extend(f"#{(' ' + line) if line else ''}" for line in LEDGER_HEADER)
    for entry in sorted(records, key=lambda item: item.path):
        # One blank line between records. Measured, not cosmetic: Git conflicts
        # when the two sides' changed regions TOUCH, so two branches accepting
        # documents whose ledger lines are neighbours (alpha.md / beta.md) still
        # collided without the separator. One unchanged line between them is
        # enough for a clean auto-merge — see the adjacent-documents merge test.
        lines.append("")
        lines.append(f"{entry.path}\t{entry.digest}\t{entry.accepted_for}")
    return "\n".join(lines) + "\n"


def write_ledger(
    path: Path | str,
    digests: Mapping[str, str],
    accepted_for: str = "",
    previous: Mapping[str, LedgerEntry] | None = None,
) -> Path:
    """Write the ledger deterministically.

    ``accepted_for`` is stamped on every document whose digest is new or changed.
    A document whose digest is unchanged KEEPS the reference of the accept that
    originally recorded it — re-stamping every line on each accept would churn
    the whole file, destroy ``git blame`` for the untouched entries, and turn the
    merge-friendly format back into a conflict generator.
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    prior = dict(previous or {})
    entries: dict[str, LedgerEntry] = {}
    for rel_path in sorted(digests):
        digest = digests[rel_path]
        carried = prior.get(rel_path)
        keep_reference = (
            carried is not None and carried.digest == digest and carried.accepted_for
        )
        entries[rel_path] = LedgerEntry(
            path=rel_path,
            digest=digest,
            accepted_for=carried.accepted_for if keep_reference else accepted_for,
        )
    lock_path.write_text(render_ledger(entries), encoding="utf-8")
    return lock_path


# ═══════════════════════════════════════════════════════════
# Evaluation
# ═══════════════════════════════════════════════════════════


def evaluate_canon(
    project_root: Path | str,
    config: Mapping[str, Any] | None = None,
) -> CanonStatus:
    """Compare the ledger against the documents on disk.

    Never raises for the ordinary failure modes (no config dir, no ledger,
    corrupt ledger): each is reported as a field on the returned status, so a
    caller can decide the severity rather than crashing a verify run.
    """
    root = Path(project_root)
    settings = canon_settings(config)
    lock_path = canon_lock_path(root)
    rel_lock = _relative_posix(root, lock_path) if lock_path is not None else None

    if not settings.enabled:
        return CanonStatus(enabled=False, ledger_path=rel_lock)

    documents = canon_documents(root, config)
    current = compute_digests(root, documents)

    if lock_path is None:
        return CanonStatus(
            ledger_present=False,
            ledger_path=None,
            untracked=sorted(current),
        )

    try:
        payload = load_ledger(lock_path)
    except ValueError as exc:
        return CanonStatus(
            ledger_present=True,
            ledger_path=rel_lock,
            ledger_unreadable=str(exc),
            untracked=sorted(current),
        )

    if payload is None:
        return CanonStatus(
            ledger_present=False,
            ledger_path=rel_lock,
            untracked=sorted(current),
        )

    recorded_version = payload.get("version")
    if not isinstance(recorded_version, int) or recorded_version > LEDGER_VERSION:
        # A ledger written by a NEWER CoDD must not be reinterpreted against an
        # older schema — that would produce confident nonsense. An OLDER version
        # is read normally (see _load_legacy_json_ledger): resurrecting the "no
        # ledger" advisory for a project that had already adopted the mechanism
        # would be a regression dressed as a warning.
        return CanonStatus(
            ledger_present=True,
            ledger_path=rel_lock,
            ledger_unreadable=(
                f"unsupported ledger version {recorded_version!r} "
                f"(this CoDD understands version {LEDGER_VERSION} and older)"
            ),
        )

    recorded = ledger_digests(payload)

    modified: list[str] = []
    missing: list[str] = []
    verified: list[str] = []
    for rel, recorded_digest in sorted(recorded.items()):
        current_digest = current.get(rel)
        if current_digest is None:
            missing.append(rel)
        elif current_digest != recorded_digest:
            modified.append(rel)
        else:
            verified.append(rel)

    untracked = sorted(rel for rel in current if rel not in recorded)

    return CanonStatus(
        ledger_present=True,
        ledger_path=rel_lock,
        modified=modified,
        missing=missing,
        untracked=untracked,
        verified=verified,
    )
