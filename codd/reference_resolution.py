"""Deterministic reference-resolution contract for CoDD artifact references.

ACG axis-1 ("reference supply-demand coherence"): whenever artifact A names
artifact B (e.g. an implement task naming its ``source_design_doc``), that
reference is a *demand edge*. The edge must bind to **exactly one** registered
artifact identity before it can influence generation, repair, verification, or
green status — it must never travel downstream as a raw, possibly-broken
string.

This module provides the SHARED, deterministic resolver used at both
ingestion-time (``plan_deriver`` canonicalizes SUT-supplied refs before they
are persisted) and read-time (``implementer`` resolves the stored/legacy ref
when it reads a design doc).

Determinism is the safety contract. There is **NO fuzzy matching** here — no
Levenshtein, no partial/substring match, no stem similarity. The only
"recovery" allowed is *unique basename recovery*, and only when the raw ref is
basename-only or doc-root + basename form (it does not assert a concrete, and
possibly wrong, subcategory). Anything ambiguous or unresolved is an honest
failure (raised as :class:`FileNotFoundError` so existing
``except FileNotFoundError`` call sites keep working). Silent binding to a
guessed artifact is forbidden, and every recovery / failure is recorded to the
audit sink.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from codd.path_safety import resolve_project_path

if TYPE_CHECKING:  # pragma: no cover - typing only
    from codd.scanner import DocumentEntry, DocumentReferenceIndex


# ═══════════════════════════════════════════════════════════
# Binding + failure types
# ═══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ReferenceBinding:
    """A resolved binding from a raw reference to one canonical artifact."""

    raw: str
    ref_kind: str
    canonical_id: str
    canonical_path: str | None
    artifact_type: str
    method: str
    recovered: bool
    producer: str | None
    candidates: tuple[str, ...] = ()


class ReferenceResolutionError(FileNotFoundError):
    """A reference could not be bound to exactly one registered artifact.

    Subclasses :class:`FileNotFoundError` so existing call sites that catch
    ``FileNotFoundError`` (``implementer._resolve_design_path`` consumers,
    ``plan derive`` CLI) keep catching it unchanged.
    """

    def __init__(self, reason: str, candidates: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.candidates = tuple(candidates)
        message = f"reference resolution failed ({reason})"
        if self.candidates:
            message += f"; candidates: {', '.join(self.candidates)}"
        super().__init__(message)


def _fail(reason: str, candidates: tuple[str, ...] = ()) -> "ReferenceResolutionError":
    return ReferenceResolutionError(reason, candidates=tuple(candidates))


# ═══════════════════════════════════════════════════════════
# Text / path normalization helpers
# ═══════════════════════════════════════════════════════════


def normalize_text(raw_ref: str) -> str:
    """Strip surrounding whitespace and surrounding quotes from a raw ref."""
    text = str(raw_ref or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


def _normalize_posix(raw: str) -> str:
    """Normalize backslashes to forward slashes and collapse ``.``/redundant parts.

    Does NOT resolve ``..`` away (that would mask an escape attempt); callers
    detect escapes separately via :func:`_escapes_project`.
    """
    candidate = raw.replace("\\", "/")
    return PurePosixPath(candidate).as_posix()


def _escapes_project(norm: str, raw: str) -> bool:
    """True when the (relative) reference LEXICALLY tries to leave the project tree.

    Fast, pure-string pre-check (Windows drive prefix / absolute / ``..``). It is
    NOT the whole jail: a path that is lexically in-root may still escape via an
    in-root SYMLINK whose target leaves the tree. That symlink-resolving
    confinement is enforced separately by :func:`resolve_project_path`
    (``path_safety``) wherever this resolver actually binds a file — at the
    exact project-relative path step (inline, in :func:`resolve_document_ref`)
    and in :func:`_ensure_inside_project` (the absolute-path step) — so the
    resolver is unified on the one symlink-aware closure rather than trusting
    strings.
    """
    if not norm:
        return False
    # Windows drive prefix e.g. "C:/..." or "C:\\...".
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        return True
    # Absolute-like.
    if norm.startswith("/"):
        return True
    parts = PurePosixPath(norm).parts
    return ".." in parts


# ═══════════════════════════════════════════════════════════
# Basename recovery guard (anti-false-green safety floor)
# ═══════════════════════════════════════════════════════════


def basename_recovery_allowed(
    norm_ref: str,
    entry: "DocumentEntry",
    doc_roots: tuple[str, ...],
) -> bool:
    """Whether unique-basename recovery may bind ``norm_ref`` to ``entry``.

    Recovery is allowed ONLY when the raw reference is "basename-only" or
    "doc_root + basename" form — i.e. it does NOT assert a concrete (and
    possibly wrong) subcategory under a document root.

    The doc actually lives at ``docs/design/api_interface_contract.md``:

    * ``api_interface_contract.md`` — basename only → allowed.
    * ``docs/api_interface_contract.md`` — doc_root ``docs/`` + basename, no
      subcategory → allowed.
    * ``docs/test/api_interface_contract.md`` — asserts subdir ``test/`` which
      is NOT the real ``design/`` → NOT allowed → honest-fail (the SUT may be
      hallucinating a *different* document).

    This guard is the anti-false-green safety floor: a weak SUT may be lifted
    to green by an *unambiguous* recovery, but never by guessing past a
    concrete, conflicting subpath.
    """
    name = PurePosixPath(norm_ref).name
    if not name:
        return False

    # Form 1: basename only (no directory component at all).
    if norm_ref == name:
        return True

    parent = PurePosixPath(norm_ref).parent.as_posix()
    if parent in ("", "."):
        return True

    # Form 2: doc_root + basename (exactly one level: the configured doc root,
    # with nothing asserted between the root and the file). The parent dir of
    # the reference must be a configured document root.
    normalized_roots = {_strip_trailing_slash(_normalize_posix(root)) for root in doc_roots}
    normalized_roots.discard("")
    if parent in normalized_roots:
        return True

    return False


def _strip_trailing_slash(value: str) -> str:
    return value[:-1] if value.endswith("/") else value


# ═══════════════════════════════════════════════════════════
# Core resolver
# ═══════════════════════════════════════════════════════════


def resolve_document_ref(
    raw_ref: str,
    *,
    project_root: Path,
    index: "DocumentReferenceIndex",
    producer: str | None,
    ref_kind: str,
    allow_recovery: bool = True,
) -> ReferenceBinding:
    """Bind a document reference to exactly one registered document.

    Resolution ladder (deterministic, fail-closed). Raises
    :class:`ReferenceResolutionError` (a ``FileNotFoundError``) when the
    reference cannot bind to a single registered document.
    """
    raw = normalize_text(raw_ref)
    if not raw:
        raise _fail("empty_reference")
    if "\x00" in raw:
        raise _fail("invalid_reference")

    # 2. Absolute path → must be inside project AND an existing file.
    candidate = Path(raw)
    if candidate.is_absolute():
        _ensure_inside_project(project_root, candidate)
        if candidate.is_file():
            return _bind_path(
                raw=raw,
                ref_kind=ref_kind,
                producer=producer,
                project_root=project_root,
                index=index,
                file_path=candidate,
                method="absolute_exact_path",
            )
        raise _fail("unresolved_absolute_path")

    # 3. Normalize to posix; reject escapes.
    norm = _normalize_posix(raw)
    if _escapes_project(norm, raw):
        raise _fail("reference_escapes_project")

    # 4. Classify.
    path_like = ("/" in norm) or ("\\" in raw) or norm.endswith(".md")

    # 5. Exact project-relative path.
    if path_like:
        project_relative = project_root / norm
        if project_relative.is_file():
            # Symlink-aware confinement (unified on path_safety): a lexically
            # in-root ref whose FILE is an in-root symlink pointing OUTSIDE the
            # tree must not bind (it would consume an off-root file as evidence —
            # a path-escape false-green the string-only ``_escapes_project``
            # cannot see).
            if resolve_project_path(project_root, project_relative) is None:
                raise _fail("reference_escapes_project")
            entry = index.by_path.get(norm)
            if entry is not None:
                return _bind_entry(
                    raw=raw, ref_kind=ref_kind, producer=producer, entry=entry, method="exact_path"
                )
            # The file exists exactly as referenced but carries no CoDD
            # frontmatter (unregistered). This is NOT a guess — the SUT named a
            # real, existing path — so bind it with a synthetic doc:<path>
            # identity rather than hard-failing. Honest-fail is reserved for
            # refs that do not resolve to a concrete file; hard-failing an
            # existing exact path would regress legacy/brownfield docs.
            return _bind_unregistered_path(
                raw=raw, ref_kind=ref_kind, producer=producer, rel_path=norm
            )

    # 6. Exact node_id.
    node_matches = index.by_node_id.get(raw)
    if node_matches:
        if len(node_matches) == 1:
            return _bind_entry(
                raw=raw,
                ref_kind=ref_kind,
                producer=producer,
                entry=node_matches[0],
                method="exact_node_id",
            )
        raise _fail("ambiguous_node_id", candidates=_candidate_paths(node_matches))

    # 7. Exact alias.
    alias_matches = index.by_alias.get(norm)
    if alias_matches:
        if len(alias_matches) == 1:
            return _bind_entry(
                raw=raw,
                ref_kind=ref_kind,
                producer=producer,
                entry=alias_matches[0],
                method="exact_alias",
            )
        raise _fail("ambiguous_alias", candidates=_candidate_paths(alias_matches))

    # 8. Unique basename recovery (only path-like markdown refs).
    basename = PurePosixPath(norm).name
    if allow_recovery and path_like and basename.endswith(".md"):
        matches = index.by_basename.get(basename, [])
        if len(matches) == 1 and basename_recovery_allowed(norm, matches[0], index.doc_roots):
            return _bind_entry(
                raw=raw,
                ref_kind=ref_kind,
                producer=producer,
                entry=matches[0],
                method="unique_basename_recovered",
                recovered=True,
            )
        if len(matches) > 1:
            raise _fail("ambiguous_basename", candidates=_candidate_paths(matches))

    # 9. No match.
    raise _fail("unresolved_reference")


def _ensure_inside_project(project_root: Path, path: Path) -> None:
    """Confine an ABSOLUTE reference to the project tree (symlink-aware, unified).

    Delegates to the one shared ``path_safety`` closure
    (:func:`resolve_project_path`) instead of an independent
    ``resolve()`` + ``relative_to`` reimplementation, so absolute-path
    confinement (including in-root symlinks whose target escapes) is identical to
    every other CoDD jail. ``None`` ⇒ outside root ⇒ honest fail.
    """
    if resolve_project_path(project_root, path) is None:
        raise _fail("reference_escapes_project")


def _candidate_paths(entries: list["DocumentEntry"]) -> tuple[str, ...]:
    return tuple(entry.path.as_posix() for entry in entries)


def _bind_entry(
    *,
    raw: str,
    ref_kind: str,
    producer: str | None,
    entry: "DocumentEntry",
    method: str,
    recovered: bool = False,
) -> ReferenceBinding:
    return ReferenceBinding(
        raw=raw,
        ref_kind=ref_kind,
        canonical_id=entry.node_id,
        canonical_path=entry.path.as_posix(),
        artifact_type="document",
        method=method,
        recovered=recovered,
        producer=producer,
        candidates=(entry.path.as_posix(),),
    )


def _bind_unregistered_path(
    *,
    raw: str,
    ref_kind: str,
    producer: str | None,
    rel_path: str,
) -> ReferenceBinding:
    """Bind an existing-but-unregistered file path to a synthetic ``doc:<path>``.

    Used when a reference names a real, existing file that carries no CoDD
    frontmatter. Binding (rather than failing) preserves the pre-contract
    behavior where an existing exact path always resolved; it is safe because
    the SUT named a concrete existing path — no guessing is involved. The
    synthetic ``doc:<path>`` identity matches scanner's node_id fallback.
    """
    return ReferenceBinding(
        raw=raw,
        ref_kind=ref_kind,
        canonical_id=f"doc:{rel_path}",
        canonical_path=rel_path,
        artifact_type="document",
        method="exact_path_unregistered",
        recovered=False,
        producer=producer,
        candidates=(rel_path,),
    )


def _bind_path(
    *,
    raw: str,
    ref_kind: str,
    producer: str | None,
    project_root: Path,
    index: "DocumentReferenceIndex",
    file_path: Path,
    method: str,
) -> ReferenceBinding:
    """Bind an absolute, in-project file path; prefer the registered identity."""
    try:
        rel = file_path.resolve(strict=False).relative_to(project_root.resolve(strict=False)).as_posix()
    except ValueError:
        rel = file_path.as_posix()
    entry = index.by_path.get(rel)
    if entry is None:
        return _bind_unregistered_path(
            raw=raw, ref_kind=ref_kind, producer=producer, rel_path=rel
        )
    return _bind_entry(
        raw=raw, ref_kind=ref_kind, producer=producer, entry=entry, method=method
    )


# ═══════════════════════════════════════════════════════════
# Audit sink
# ═══════════════════════════════════════════════════════════

AUDIT_RELATIVE_PATH = Path(".codd") / "audit" / "reference_resolution.jsonl"


def record_reference_resolution_event(
    project_root: Path,
    binding: ReferenceBinding | None,
    *,
    stage: str,
    status: str,
    candidates: tuple[str, ...] = (),
) -> None:
    """Append one JSON line describing a resolution outcome.

    Best-effort: never raises. Silent recovery is forbidden — callers MUST
    record recoveries and failures here. ``status`` is one of
    ``exact | recovered | ambiguous | unresolved``.
    """
    try:
        record: dict[str, object] = {
            "event": "reference_resolution",
            "stage": stage,
            "producer": binding.producer if binding is not None else None,
            "ref_kind": binding.ref_kind if binding is not None else None,
            "raw": binding.raw if binding is not None else None,
            "status": status,
            "method": binding.method if binding is not None else None,
            "canonical_id": binding.canonical_id if binding is not None else None,
            "canonical_path": binding.canonical_path if binding is not None else None,
            "candidates": list(binding.candidates if binding is not None else candidates),
        }
        audit_path = project_root / AUDIT_RELATIVE_PATH
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:  # pragma: no cover - audit is best-effort
        return


def record_resolution_failure(
    project_root: Path,
    raw: str,
    *,
    stage: str,
    ref_kind: str,
    producer: str | None,
    error: ReferenceResolutionError,
) -> None:
    """Record an honest-fail outcome (ambiguous/unresolved/etc.)."""
    reason = getattr(error, "reason", "unresolved_reference")
    status = "ambiguous" if reason.startswith("ambiguous") else "unresolved"
    try:
        record = {
            "event": "reference_resolution",
            "stage": stage,
            "producer": producer,
            "ref_kind": ref_kind,
            "raw": raw,
            "status": status,
            "method": None,
            "canonical_id": None,
            "canonical_path": None,
            "candidates": list(getattr(error, "candidates", ())),
            "reason": reason,
            "action": "honest_fail",
        }
        audit_path = project_root / AUDIT_RELATIVE_PATH
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:  # pragma: no cover - audit is best-effort
        return


def _status_for_binding(binding: ReferenceBinding) -> str:
    return "recovered" if binding.recovered else "exact"


__all__ = [
    "ReferenceBinding",
    "ReferenceResolutionError",
    "resolve_document_ref",
    "basename_recovery_allowed",
    "normalize_text",
    "record_reference_resolution_event",
    "record_resolution_failure",
]
