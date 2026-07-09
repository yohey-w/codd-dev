"""Deterministic dependency-boundary conformance check — a language-free gate.

Fable5-authorized Increment 1 (see
``dogfood/fable5_reply_2026-07-09_verify-coherence.md``, "Increment 1", Q2/Q3/Q5).
A sibling to :mod:`codd.test_import_coherence` in SHAPE (a standalone library check
mirroring ``check_test_import_coherence``), it proves a DIFFERENT invariant, over
the SOURCE tree, from the ACG's already-declared ``depends_on`` graph:

  every INTERNAL import edge a generated source file emits must land in
  {the design doc that OWNS the file} ∪ {the transitive ``depends_on``
  closure of that owning doc}.

A RESOLVED internal import to a doc PROVABLY OUTSIDE that closure is a boundary
violation — an independently-generated file reaching for a capability its own
design never declared a dependency on. This is the ts-v3 failure class, killed
deterministically at implement time (before verify), instead of relying on an
LLM repair loop to derive the fix.

ANTI-FALSE-GREEN / ANTI-FALSE-RED (load-bearing, the Python oracle's exact rule —
"PROVABLY absent → fail; unknown → never fail", ``oracle_python.py``):

  * ONLY a resolved internal import whose owning doc is PROVABLY outside a FULLY-
    DETERMINED closure is flagged. If the owning doc's closure could not be fully
    determined (a doc frontmatter that will not parse, or a ``depends_on`` ref
    that resolves to no doc), the target is NOT provably outside → NOT flagged,
    logged as residue.
  * An internal-looking specifier that resolves to NOTHING degrades to logged
    residue (the ``_add_import_edges`` precedent, ``dag/builder.py``), never a
    failure.
  * A resolved target that no derived task owns (an orphan) has no owning doc →
    unknown → not flagged (the orphan-artifact gate owns that axis).

GENERALITY (Q3): ZERO ``language ==`` / per-language literal. Layer data comes
from frontmatter ``depends_on`` (DATA); import extraction + resolution +
internal-vs-external classification dispatch through the suffix-map / shape-driven
predicates the DAG builder already owns (``_extract_impl_imports``,
``_resolve_import_targets``, ``_is_internal_looking_specifier``). Adding a language
is data, not a core branch.

SCOPE v1 = SOURCE artifacts only. Test-tree artifacts (outputs under the layout
profile's ``test_root``) are EXCLUDED — their cross-tree imports are governed by
:mod:`codd.test_import_coherence` + the closure itself. The exclusion is LOGGED
on the result (no silent cap).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

# Reuse — NOT reimplement (Q3): the SAME extractor/resolver/predicate the DAG
# builder uses for its import edges. Importing these at module load is cycle-safe:
# ``codd.dag.builder`` imports neither this module nor ``codd.implement_oracle``.
from codd.dag.builder import (
    _extract_impl_imports,
    _is_internal_looking_specifier,
    _load_import_aliases,
    _resolve_import_targets,
)


__all__ = [
    "DependencyBoundaryFinding",
    "DependencyBoundaryResult",
    "check_dependency_boundary_coherence",
]


@dataclass(frozen=True)
class DependencyBoundaryFinding:
    """One dependency-boundary violation, with a precise message.

    ``path`` is the importing SOURCE file; ``specifier`` is its raw import; the
    edge ``owning_doc`` → ``target_doc`` is the missing ``depends_on`` edge.
    """

    path: str
    specifier: str
    owning_doc: str
    target_doc: str
    target_path: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DependencyBoundaryResult:
    """Outcome of the dependency-boundary conformance check."""

    passed: bool
    findings: list[DependencyBoundaryFinding] = field(default_factory=list)
    #: Unresolvable internal-looking specifiers, and undecidable-closure edges.
    #: Surfaced (logged), NEVER a failure (anti-false-RED).
    residue: list[str] = field(default_factory=list)
    #: Source-only scope: owned outputs under the test root, excluded here and
    #: covered by ``test_import_coherence``. Logged — no silent cap.
    excluded_test_artifacts: list[str] = field(default_factory=list)
    detail: str = ""

    def summary(self) -> str:
        if self.passed:
            return self.detail or "dependency-boundary: OK"
        lines = [f"dependency-boundary gate FAILED ({len(self.findings)} finding(s)):"]
        for finding in self.findings:
            lines.append(f"  - {finding.path}: {finding.message}")
        return "\n".join(lines)


def _norm_rel(text: Any) -> str:
    """Normalize a path-ish reference: forward slashes, no leading ``./`` or ``/``."""
    value = str(text or "").strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def _safe_node_path_map(project_root: Path, config: Mapping[str, Any]) -> dict[str, Path]:
    """``node_id -> rel Path`` for every registered design doc (best-effort).

    Lets a doc reference given as a ``node_id`` (``design:consumer``) canonicalize
    to the same doc PATH a reference given as a path would. Any failure (a config
    without ``scan`` data, a scan error) degrades to an empty map — path-form
    references still resolve directly, so the check keeps working.
    """
    try:
        from codd.scanner import build_document_node_path_map

        return build_document_node_path_map(project_root, dict(config or {}))
    except Exception:  # noqa: BLE001 — node-id resolution is an enrichment, not a dependency.
        return {}


def _canonical_doc(ref: Any, project_root: Path, node_path_map: Mapping[str, Path]) -> str | None:
    """Canonicalize a doc reference (node_id OR path) to a project-relative doc path.

    Returns ``None`` when the reference resolves to no known doc file — an
    UNKNOWN owner, which the caller treats conservatively (never a failure).
    """
    text = str(ref or "").strip()
    if not text:
        return None
    # node_id form (``design:consumer``) → its registered path.
    mapped = node_path_map.get(text)
    if mapped is not None:
        return mapped.as_posix()
    # path form (``docs/design/consumer.md``) → itself, iff the file exists.
    norm = _norm_rel(text)
    if norm and (project_root / norm).is_file():
        return norm
    return None


def _extract_frontmatter_safe(doc_path: Path) -> tuple[dict | None, bool]:
    """``(codd_frontmatter, ok)``: ok=False when the doc could not be parsed."""
    try:
        from codd.scanner import _extract_frontmatter

        return _extract_frontmatter(doc_path), True
    except Exception:  # noqa: BLE001 — an unparseable doc = undecidable, not a crash.
        return None, False


def _normalize_depends_on(codd: Mapping[str, Any]) -> tuple[list[str], bool]:
    """``(dep_ids, ok)`` from a doc's ``depends_on`` frontmatter (ok=False on error)."""
    try:
        from codd.generator import _normalize_dependencies

        return [dep["id"] for dep in _normalize_dependencies(codd.get("depends_on", []))], True
    except Exception:  # noqa: BLE001 — a malformed depends_on = undecidable, not a crash.
        return [], False


def _closure(
    owner_key: str,
    project_root: Path,
    node_path_map: Mapping[str, Path],
    cache: dict[str, tuple[frozenset[str], bool]],
) -> tuple[frozenset[str], bool]:
    """The owner doc's ``{self} ∪ transitive depends_on closure`` + a CERTAINTY flag.

    BFS over frontmatter ``depends_on`` (the exact machinery
    ``implementer._collect_dependency_documents`` walks). ``certain`` is False when
    ANY doc in the walk could not be parsed, had no readable frontmatter, or named
    a ``depends_on`` target that resolves to no doc — because then a target NOT in
    the known set is not PROVABLY outside the closure (anti-false-RED).
    """
    if owner_key in cache:
        return cache[owner_key]

    seen: set[str] = set()
    certain = True
    queue: deque[str] = deque([owner_key])
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)

        codd, ok = _extract_frontmatter_safe(project_root / current)
        if not ok or codd is None:
            # Cannot read this doc's declared dependencies → closure is undecidable.
            certain = False
            continue
        dep_ids, deps_ok = _normalize_depends_on(codd)
        if not deps_ok:
            certain = False
            continue
        for dep_id in dep_ids:
            dep_key = _canonical_doc(dep_id, project_root, node_path_map)
            if dep_key is None:
                # A depends_on target we cannot map to a doc: the checked target
                # MIGHT be this same edge under another spelling → not provable.
                certain = False
                continue
            if dep_key not in seen:
                queue.append(dep_key)

    result = (frozenset(seen), certain)
    cache[owner_key] = result
    return result


def _concrete_source_files(project_root: Path, output: str) -> list[str]:
    """Project-relative files an ``expected_outputs`` entry names, that exist on disk.

    A concrete file → itself; a directory → the code files under it (bounded to the
    project tree); a not-yet-generated / glob entry → nothing.
    """
    norm = _norm_rel(output)
    if not norm:
        return []
    target = project_root / norm
    if target.is_file():
        return [norm]
    if target.is_dir():
        files: list[str] = []
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            if any(part == "__pycache__" or part.startswith(".") for part in path.parts):
                continue
            try:
                files.append(path.resolve().relative_to(project_root.resolve()).as_posix())
            except (ValueError, OSError):
                continue
        return files
    return []


def _test_root_prefixes(
    profile: Any,
    test_dirs: Any,
    config: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """Normalized test-root prefixes for the SOURCE-only scope (data, not language).

    Prefer the resolved layout profile's ``test_root`` (derived from the project's
    own ``scan.test_dirs`` DATA); fall back to an explicit ``test_dirs`` arg or the
    config's ``scan.test_dirs``. Empty when none is known — then nothing is treated
    as a test artifact.
    """
    roots: list[str] = []
    profile_test_root = getattr(profile, "test_root", None)
    if profile_test_root:
        roots.append(_norm_rel(profile_test_root))
    for source in (test_dirs, _config_test_dirs(config)):
        if isinstance(source, str):
            roots.append(_norm_rel(source))
        elif isinstance(source, (list, tuple, set)):
            roots.extend(_norm_rel(item) for item in source)
    return tuple(sorted({root for root in roots if root}))


def _config_test_dirs(config: Mapping[str, Any] | None) -> Any:
    section = (config or {}).get("scan") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        return section.get("test_dirs")
    return None


def _under_any(rel: str, prefixes: Sequence[str]) -> bool:
    rel_parts = PurePosixPath(rel).parts
    for prefix in prefixes:
        prefix_parts = PurePosixPath(prefix).parts
        if prefix_parts and rel_parts[: len(prefix_parts)] == prefix_parts:
            return True
    return False


def _resolve_profile(
    project_root: Path,
    *,
    language: str | None,
    project_name: str | None,
    source_dirs: Any,
    test_dirs: Any,
    config: Mapping[str, Any] | None,
) -> Any:
    """Resolve a layout profile by language (DATA dispatch) for test-scope exclusion.

    Best-effort: a stack with no profile simply yields ``None`` (no test-root
    exclusion is applied — every owned output is treated as source).
    """
    try:
        from codd.project_types import resolve_layout_profile

        return resolve_layout_profile(
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
            project_root=project_root,
        )
    except Exception:  # noqa: BLE001 — profile resolution is for exclusion only.
        return None


def check_dependency_boundary_coherence(
    project_root: Path | str,
    *,
    language: str | None = None,
    project_name: str | None = None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Mapping[str, Any] | None = None,
    profile: Any = None,
) -> DependencyBoundaryResult:
    """Prove every generated SOURCE file's internal imports stay within its closure.

    Returns a passing no-op when there are no derived tasks (no owner map) — there
    is nothing to attribute. Otherwise every owned SOURCE file's internal import
    edges are resolved and checked against {owning doc} ∪ {closure(owning doc)};
    a resolved edge to a doc PROVABLY outside a fully-determined closure is a
    finding, everything uncertain is residue (never a failure).
    """
    root = Path(project_root)
    settings = dict(config or {})

    records = _load_derived_records(root)
    if not records:
        return DependencyBoundaryResult(
            passed=True, detail="dependency-boundary: no derived tasks (skipped)"
        )

    if profile is None:
        profile = _resolve_profile(
            root,
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
        )
    test_prefixes = _test_root_prefixes(profile, test_dirs, config)

    # 1. Owner map (source file → owning doc ref) + the owned SOURCE file set.
    owner_by_file: dict[str, str] = {}
    source_files: list[str] = []
    excluded: list[str] = []
    for _cache_path, record in records:
        for task in record.tasks:
            if not task.approved:
                continue
            owner_ref = str(task.source_design_doc or "").strip()
            for output in task.expected_outputs:
                for rel in _concrete_source_files(root, output):
                    if _under_any(rel, test_prefixes):
                        if rel not in excluded:
                            excluded.append(rel)
                        continue
                    if rel not in owner_by_file:
                        owner_by_file[rel] = owner_ref
                        source_files.append(rel)

    if not source_files:
        return DependencyBoundaryResult(
            passed=True,
            excluded_test_artifacts=excluded,
            detail="dependency-boundary: no owned source files on disk (skipped)",
        )

    node_path_map = _safe_node_path_map(root, settings)
    aliases = _safe_aliases(root, settings)

    # Resolution map: absolute file path → project-relative posix (the "node id").
    path_to_node: dict[Path, str] = {}
    for rel in source_files:
        path_to_node[(root / rel).resolve()] = rel

    findings: list[DependencyBoundaryFinding] = []
    residue: list[str] = []
    seen_violations: set[tuple[str, str, str]] = set()
    closure_cache: dict[str, tuple[frozenset[str], bool]] = {}

    for rel in sorted(source_files):
        owner_key = _canonical_doc(owner_by_file.get(rel), root, node_path_map)
        if owner_key is None:
            # The importer's own doc is unresolvable → closure undecidable → skip.
            residue.append(f"{rel}: owning doc {owner_by_file.get(rel)!r} unresolved")
            continue
        allowed, certain = _closure(owner_key, root, node_path_map, closure_cache)
        file_abs = (root / rel).resolve()

        for specifier in _safe_extract_imports(file_abs):
            targets = _safe_resolve_targets(specifier, file_abs, root, path_to_node, aliases)
            if not targets:
                # Unresolvable — residue ONLY when the specifier is internal-looking
                # (the ``_add_import_edges`` residue policy); external ones are noise.
                if _is_internal_looking_specifier(specifier, aliases):
                    residue.append(f"{rel}: {specifier} (unresolved internal specifier)")
                continue
            for target_rel in targets:
                if target_rel == rel:
                    continue  # a file importing itself is not a boundary edge
                target_key = _canonical_doc(owner_by_file.get(target_rel), root, node_path_map)
                if target_key is None:
                    # Resolved to a file no task owns (orphan) or an unresolvable
                    # owner → unknown owning doc → not provably outside → residue.
                    residue.append(f"{rel}: {specifier} -> {target_rel} (target owner unknown)")
                    continue
                if target_key in allowed:
                    continue  # same doc OR within the transitive closure → allowed
                if not certain:
                    # Closure undecidable → cannot PROVE the target is outside it.
                    residue.append(
                        f"{rel}: {specifier} -> {target_key} (owning-doc closure undecidable)"
                    )
                    continue
                dedup = (rel, specifier, target_key)
                if dedup in seen_violations:
                    continue
                seen_violations.add(dedup)
                findings.append(
                    _make_finding(rel, specifier, owner_key, target_key, target_rel, sorted(allowed))
                )

    passed = not findings
    detail = (
        f"dependency-boundary: OK ({len(source_files)} source file(s) checked)"
        if passed
        else f"dependency-boundary: {len(findings)} violation(s)"
    )
    return DependencyBoundaryResult(
        passed=passed,
        findings=findings,
        residue=residue,
        excluded_test_artifacts=excluded,
        detail=detail,
    )


def _make_finding(
    rel: str,
    specifier: str,
    owner_key: str,
    target_key: str,
    target_rel: str,
    allowed: list[str],
) -> DependencyBoundaryFinding:
    message = (
        f"source file '{rel}' (owned by design doc '{owner_key}') imports "
        f"'{specifier}', which resolves to '{target_rel}' owned by design doc "
        f"'{target_key}'. '{target_key}' is neither '{owner_key}' nor in its "
        f"transitive depends_on closure {allowed}. This import crosses a DECLARED "
        f"dependency boundary — the missing edge is '{owner_key}' -> '{target_key}'. "
        f"Either import this capability from a doc ALREADY in the closure that "
        f"provides it, or (if none does) add a 'depends_on' edge "
        f"'{owner_key}' -> '{target_key}'; do NOT inline or duplicate the code to "
        f"dodge the boundary."
    )
    return DependencyBoundaryFinding(
        path=rel,
        specifier=specifier,
        owning_doc=owner_key,
        target_doc=target_key,
        target_path=target_rel,
        message=message,
        details={
            "specifier": specifier,
            "owning_doc": owner_key,
            "target_doc": target_key,
            "target_path": target_rel,
            "missing_edge": f"{owner_key} -> {target_key}",
        },
    )


def _load_derived_records(project_root: Path) -> list:
    try:
        from codd.llm.plan_deriver import iter_derived_task_records

        return iter_derived_task_records(project_root)
    except Exception:  # noqa: BLE001 — no readable derived tasks = nothing to check.
        return []


def _safe_aliases(project_root: Path, settings: Mapping[str, Any]) -> dict[str, list[str]]:
    try:
        return _load_import_aliases(project_root, dict(settings))
    except Exception:  # noqa: BLE001 — aliases are an enrichment; absence is fine.
        return {}


def _safe_extract_imports(file_abs: Path) -> list[str]:
    try:
        return _extract_impl_imports(file_abs)
    except Exception:  # noqa: BLE001 — an unreadable file yields no edges (residue-neutral).
        return []


def _safe_resolve_targets(
    specifier: str,
    file_abs: Path,
    project_root: Path,
    path_to_node: dict[Path, str],
    aliases: dict[str, list[str]],
) -> list[str]:
    try:
        return [
            target
            for target in _resolve_import_targets(specifier, file_abs, project_root, path_to_node, aliases)
            if target
        ]
    except Exception:  # noqa: BLE001 — a resolution miss is unresolved (residue), never a crash.
        return []
