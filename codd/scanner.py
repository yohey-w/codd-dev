"""CoDD Scanner — Extract dependency data from document frontmatter + source code.

Design principle: Documents ARE the data (Single Source of Truth).
Dependency metadata is embedded as YAML frontmatter in deliverable documents.
Auto-generated data (frontmatter, AST) is refreshed on scan.
Human knowledge (manual annotations, overrides) is NEVER deleted.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from codd.discovery import scan_exclude_patterns
from codd.frontmatter import as_list, read_frontmatter
from codd.graph import CEG
from codd.parsing import get_extractor
from codd.path_safety import require_project_path, resolve_project_path


def run_scan(project_root: Path, codd_dir: Path):
    """Scan all project documents and source code, refresh auto-generated data.

    Human knowledge (source_type='human') is preserved.
    Auto-generated data (frontmatter, static, framework) is purged and rebuilt.
    """
    config_path = codd_dir / "codd.yaml"
    if not config_path.exists():
        print("Error: codd/codd.yaml not found.")
        raise SystemExit(1)

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    scan_dir = codd_dir / "scan"

    ceg = CEG(scan_dir)

    # Purge auto-generated data, keep human knowledge
    purged = ceg.purge_auto_generated()
    human_count = ceg.count_human_evidence()
    print(f"Purged auto-generated: {purged['evidence']} evidence, {purged['edges']} edges, {purged['nodes']} nodes")
    if human_count > 0:
        print(f"Preserved: {human_count} human evidence records")

    # Phase 1: Scan document frontmatter (all .md/.yaml in doc_dirs)
    scan_config = config.get("scan") or {}
    doc_dirs = scan_config.get("doc_dirs", [])
    frontmatter_count = 0
    warnings = []
    for doc_dir in doc_dirs:
        # RC-1 path-escape jail: ``scan.doc_dirs`` is user-controllable
        # (codd.yaml). A configured doc ROOT that escapes the project (absolute /
        # ``../`` / an in-root symlink whose target leaves the tree) is INVALID
        # evidence — FAIL-CLOSED (``require_project_path`` raises PathEscapeError)
        # rather than silently skip the entry and let the scan "succeed" reading
        # nothing (a silent skip is a false-green in another form: GPT). A
        # NON-EXISTENT in-root dir is NOT an escape — it stays a benign skip.
        full_path = require_project_path(project_root, doc_dir, context="scan.doc_dirs")
        if full_path.exists():
            count, doc_warnings = _scan_frontmatter(ceg, project_root, full_path)
            frontmatter_count += count
            warnings.extend(doc_warnings)

    # Phase 1b: Also scan codd/annotations/ if it exists (backward compat)
    annotations_dir = codd_dir / "annotations"
    if annotations_dir.exists():
        _load_legacy_annotations(ceg, annotations_dir)

    # Phase 2: Scan source code (imports, calls)
    language = (config.get("project") or {}).get("language", "python")
    source_dirs = scan_config.get("source_dirs", [])
    exclude_patterns = scan_exclude_patterns(config)

    for src_dir in source_dirs:
        # RC-1 path-escape jail (see doc_dirs above): a configured
        # ``scan.source_dirs`` root that escapes the project is INVALID evidence —
        # FAIL-CLOSED (raise) rather than silently skipped, before any walk/read.
        full_path = require_project_path(project_root, src_dir, context="scan.source_dirs")
        if full_path.exists():
            _scan_source_directory(ceg, project_root, full_path, language, exclude_patterns)

    # Phase 3: Extract endpoints from filesystem-based routing conventions.
    fs_route_configs = config.get("filesystem_routes", [])
    if fs_route_configs:
        _scan_filesystem_routes(ceg, project_root, fs_route_configs)

    warnings.extend(_collect_wave_config_warnings(project_root, config))
    for warning in warnings:
        print(f"WARNING: {warning}")

    stats = ceg.stats()
    print(f"Scan complete:")
    print(f"  Documents with frontmatter: {frontmatter_count}")
    print(f"  Graph: {stats['nodes']} nodes, {stats['edges']} edges")
    print(f"  Evidence: {stats['evidence']} total ({stats['human_evidence']} human, {stats['evidence'] - stats['human_evidence']} auto)")
    ceg.close()


# ═══════════════════════════════════════════════════════════
# Phase 1: Document frontmatter scanning
# ═══════════════════════════════════════════════════════════

def _scan_frontmatter(ceg: CEG, project_root: Path, doc_dir: Path) -> tuple[int, list[str]]:
    """Scan all Markdown files in a directory for CoDD frontmatter."""
    count = 0
    warnings: list[str] = []
    for root, dirs, files in os.walk(doc_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            full = Path(root) / fname
            # Re-confine each walked file: an in-root symlink (or a path that
            # otherwise resolves outside the tree) must not be read as a project
            # doc. Used purely as an escape GATE — the project-relative ``rel``
            # below keeps its original (unresolved-root) derivation so in-root
            # paths/warnings are byte-identical to before (anti-false-red).
            if resolve_project_path(project_root, full) is None:
                continue
            rel = full.relative_to(project_root).as_posix()
            codd_data = _extract_frontmatter(full)
            if codd_data:
                _load_frontmatter(ceg, rel, codd_data)
                count += 1
                warnings.extend(_collect_document_warnings(rel, codd_data))
            elif rel.startswith("docs/"):
                warnings.append(f"{rel}: missing CoDD YAML frontmatter")
    if count > 0:
        print(f"  Frontmatter: {count} documents in {doc_dir.relative_to(project_root)}")
    return count, warnings


def _extract_frontmatter(file_path: Path) -> dict | None:
    """Extract CoDD metadata from Markdown YAML frontmatter.

    Supports:
      ---
      codd:
        node_id: "req:FR-03"
        ...
      ---
      # Document content
    """
    frontmatter = read_frontmatter(file_path)
    if frontmatter is None:
        return None
    return frontmatter.get("codd")


# ═══════════════════════════════════════════════════════════
# Document Reference Index (ACG axis-1 reference resolution)
# ═══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class DocumentEntry:
    """One registered document: its node id, canonical path, and roots."""

    node_id: str
    path: Path  # project-relative
    basename: str
    doc_root: str  # the configured ``doc_dirs`` root this was found under


@dataclass(frozen=True)
class ReferenceCollision:
    """Two registered documents sharing a key (node_id / alias)."""

    kind: str  # "node_id" | "alias"
    key: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class DocumentReferenceIndex:
    """Multi-key index of registered documents for deterministic resolution.

    * ``by_path`` — canonical project-relative POSIX path → entry (unique).
    * ``by_node_id`` — node id → LIST of entries (duplicates recorded, never
      silently overwritten).
    * ``by_basename`` — filename → LIST of entries.
    * ``by_alias`` — alias (canonical rel path, ``doc:<rel>``) → LIST of
      entries.
    * ``doc_roots`` — the configured ``scan.doc_dirs`` roots (normalized).
    * ``collisions`` — recorded node_id/alias collisions.
    """

    by_path: dict[str, DocumentEntry]
    by_node_id: dict[str, list[DocumentEntry]]
    by_basename: dict[str, list[DocumentEntry]]
    by_alias: dict[str, list[DocumentEntry]]
    doc_roots: tuple[str, ...]
    collisions: list[ReferenceCollision] = field(default_factory=list)


def build_document_reference_index(
    project_root: Path, config: dict[str, Any]
) -> DocumentReferenceIndex:
    """Build the multi-key document reference index from ``scan.doc_dirs``.

    Walks the same source as :func:`build_document_node_path_map` (every ``.md``
    under each configured ``doc_dirs`` root that carries CoDD frontmatter) plus
    wave artifacts, and registers each document by path, node_id (list),
    basename (list), and alias (list). Duplicate node_ids/aliases are recorded
    as collisions rather than overwritten, so the resolver can fail-closed on
    ambiguity.
    """
    by_path: dict[str, DocumentEntry] = {}
    by_node_id: dict[str, list[DocumentEntry]] = {}
    by_basename: dict[str, list[DocumentEntry]] = {}
    by_alias: dict[str, list[DocumentEntry]] = {}
    collisions: list[ReferenceCollision] = []

    raw_doc_roots = list(config.get("scan", {}).get("doc_dirs", []))
    doc_roots = tuple(Path(root).as_posix().rstrip("/") for root in raw_doc_roots)

    def _register(entry: DocumentEntry) -> None:
        rel_posix = entry.path.as_posix()
        # by_path is filesystem-unique; first registration wins.
        by_path.setdefault(rel_posix, entry)
        by_basename.setdefault(entry.basename, []).append(entry)

        existing_node = by_node_id.setdefault(entry.node_id, [])
        existing_node.append(entry)
        if len(existing_node) == 2:
            collisions.append(
                ReferenceCollision(
                    kind="node_id",
                    key=entry.node_id,
                    paths=tuple(e.path.as_posix() for e in existing_node),
                )
            )

        for alias in (rel_posix, f"doc:{rel_posix}"):
            existing_alias = by_alias.setdefault(alias, [])
            existing_alias.append(entry)
            if len(existing_alias) == 2:
                collisions.append(
                    ReferenceCollision(
                        kind="alias",
                        key=alias,
                        paths=tuple(e.path.as_posix() for e in existing_alias),
                    )
                )

    for raw_root in raw_doc_roots:
        # RC-1 path-escape jail: this index IS the document node→path map the
        # implementer / generator / assembler consume. ``scan.doc_dirs`` is
        # user-controllable, so a configured doc ROOT that escapes the project
        # (absolute / ``../`` / in-root symlink leaving the tree) is INVALID
        # evidence — FAIL-CLOSED (raise) rather than silently skipped, so a
        # smuggled off-root tree can never be passed off as "no docs found" while
        # the reference map is built as if valid. A non-existent in-root root
        # stays a benign skip.
        full_path = require_project_path(project_root, raw_root, context="scan.doc_dirs")
        if not full_path.exists():
            continue
        doc_root_posix = Path(raw_root).as_posix().rstrip("/")

        for root, _, files in os.walk(full_path):
            for fname in files:
                if not fname.endswith(".md"):
                    continue

                full = Path(root) / fname
                # Re-confine each walked file (in-root symlink escape gate);
                # ``rel`` keeps its original derivation for in-root files so the
                # node ids/paths are byte-identical (anti-false-red).
                if resolve_project_path(project_root, full) is None:
                    continue
                rel = full.relative_to(project_root)
                codd_data = _extract_frontmatter(full)
                if not codd_data:
                    continue

                node_id = str(codd_data.get("node_id", f"doc:{rel.as_posix()}"))
                _register(
                    DocumentEntry(
                        node_id=node_id,
                        path=rel,
                        basename=rel.name,
                        doc_root=doc_root_posix,
                    )
                )

    from codd.generator import _load_wave_artifacts

    try:
        artifacts = _load_wave_artifacts(config)
    except ValueError:
        artifacts = []

    for artifact in artifacts:
        # Backward-compat semantics: wave artifacts only contribute a node_id
        # mapping if that node_id isn't already registered (mirrors the old
        # ``setdefault`` behavior in build_document_node_path_map).
        if artifact.node_id in by_node_id:
            continue
        artifact_path = Path(artifact.output)
        _register(
            DocumentEntry(
                node_id=artifact.node_id,
                path=artifact_path,
                basename=artifact_path.name,
                doc_root="",
            )
        )

    return DocumentReferenceIndex(
        by_path=by_path,
        by_node_id=by_node_id,
        by_basename=by_basename,
        by_alias=by_alias,
        doc_roots=doc_roots,
        collisions=collisions,
    )


def build_document_node_path_map(project_root: Path, config: dict[str, Any]) -> dict[str, Path]:
    """Resolve document node IDs to project-relative paths.

    Backward-compatible wrapper over :func:`build_document_reference_index`:
    derives the legacy ``node_id -> Path`` mapping. Signature and return
    semantics are unchanged (first frontmatter doc wins per node_id; wave
    artifacts fill in via ``setdefault``).
    """
    index = build_document_reference_index(project_root, config)
    node_paths: dict[str, Path] = {}
    for node_id, entries in index.by_node_id.items():
        # Preserve legacy semantics exactly: the old map did
        # ``node_paths[node_id] = rel`` for each frontmatter doc in scan order
        # (so a later duplicate frontmatter node_id overwrites an earlier one),
        # then filled wave artifacts via ``setdefault`` (so a wave node_id never
        # overwrote a frontmatter one). ``build_document_reference_index`` only
        # registers a wave artifact when its node_id is otherwise absent, so the
        # last entry in the list is the one the legacy dict would have kept.
        node_paths[node_id] = entries[-1].path
    return node_paths


def _load_frontmatter(ceg: CEG, doc_path: str, codd: dict):
    """Load CoDD frontmatter data into the graph."""
    node_id = codd.get("node_id", f"doc:{doc_path}")
    node_type = codd.get("type", "document")
    ceg.upsert_node(node_id, node_type, path=doc_path, name=node_id)

    # Process depends_on (outgoing edges from this document).
    # ``as_list`` normalizes null-vs-missing: a bare ``depends_on:`` key parses
    # to None, which would raise TypeError on iteration (issue #33). Non-dict
    # entries are malformed shape (the validator reports them); skip them here so
    # a scan never crashes on hand-authored frontmatter.
    for dep in as_list(codd.get("depends_on")):
        if not isinstance(dep, dict):
            continue
        target_id = dep.get("id")
        if not target_id:
            continue
        target_type = _infer_node_type(target_id)
        ceg.upsert_node(target_id, target_type, name=target_id)
        relation = dep.get("relation", "depends_on")
        semantic = dep.get("semantic", "governance")
        edge_id = ceg.add_edge(node_id, target_id, relation, semantic)
        ceg.add_evidence(edge_id, "frontmatter", "frontmatter", 0.9,
                         detail=f"from {doc_path}")

    # Process depended_by (incoming edges — other things that depend on this).
    # Same null-vs-missing normalization + malformed-shape skip as depends_on.
    for dep in as_list(codd.get("depended_by")):
        if not isinstance(dep, dict):
            continue
        source_id = dep.get("id")
        if not source_id:
            continue
        source_type = _infer_node_type(source_id)
        ceg.upsert_node(source_id, source_type, name=source_id)
        relation = dep.get("relation", "depends_on")
        semantic = dep.get("semantic", "governance")
        edge_id = ceg.add_edge(source_id, node_id, relation, semantic)
        ceg.add_evidence(edge_id, "frontmatter", "frontmatter", 0.9,
                         detail=f"from {doc_path}")

    # Process conventions (must_review rules embedded in document)
    for conv in codd.get("conventions", []):
        targets = conv.get("targets", [])
        if isinstance(targets, str):
            targets = [targets]
        reason = conv.get("reason", "")
        for target in targets:
            target_type = _infer_node_type(target)
            ceg.upsert_node(target, target_type, name=target)
            edge_id = ceg.add_edge(node_id, target, "must_review", "governance")
            ceg.add_evidence(edge_id, "frontmatter", "convention", 0.8, detail=reason)

    # Process data_dependencies (behavioral edges)
    for data_dep in codd.get("data_dependencies", []):
        table = data_dep.get("table", "")
        column = data_dep.get("column", "")
        dep_id = f"db_column:{table}.{column}"
        ceg.upsert_node(dep_id, "db_column", name=f"{table}.{column}")
        for affected in data_dep.get("affects", []):
            ceg.upsert_node(affected, _infer_node_type(affected), name=affected)
            edge_id = ceg.add_edge(dep_id, affected, "behavioral_dependency", "behavioral")
            ceg.add_evidence(edge_id, "frontmatter", "frontmatter", 0.75,
                             detail=data_dep.get("condition", ""))

    # R6.2: source_files bridge edges (extracted design → source file)
    for source_file in codd.get("source_files", []):
        file_node_id = f"file:{source_file}"
        ceg.upsert_node(file_node_id, "file", path=source_file, name=file_node_id)
        edge_id = ceg.add_edge(node_id, file_node_id, "extracted_from", "technical")
        ceg.add_evidence(edge_id, "frontmatter", "source_files", 0.85,
                         detail=f"design doc maps to source file {source_file}")


# ═══════════════════════════════════════════════════════════
# Legacy: annotations/ YAML support (backward compatibility)
# ═══════════════════════════════════════════════════════════

def _load_legacy_annotations(ceg: CEG, annotations_dir: Path):
    """Load legacy annotations/*.yaml files (backward compat with v0.1)."""
    loaded = False

    conv_path = annotations_dir / "conventions.yaml"
    if conv_path.exists():
        data = yaml.safe_load(conv_path.read_text(encoding="utf-8"))
        for conv in (data or {}).get("conventions", []):
            _load_legacy_convention(ceg, conv)
            loaded = True

    links_path = annotations_dir / "doc_links.yaml"
    if links_path.exists():
        data = yaml.safe_load(links_path.read_text(encoding="utf-8"))
        for link in (data or {}).get("links", []):
            _load_legacy_doc_link(ceg, link)
            loaded = True

    deps_path = annotations_dir / "data_dependencies.yaml"
    if deps_path.exists():
        data = yaml.safe_load(deps_path.read_text(encoding="utf-8"))
        for dep in (data or {}).get("data_dependencies", []):
            _load_legacy_data_dependency(ceg, dep)
            loaded = True

    if loaded:
        print("  Legacy annotations/ loaded (consider migrating to frontmatter)")


def _load_legacy_convention(ceg: CEG, conv: dict):
    sources = conv.get("when_changed", [])
    if isinstance(sources, str):
        sources = [sources]
    targets = conv.get("must_review", [])
    if isinstance(targets, str):
        targets = [targets]

    for source in sources:
        ceg.upsert_node(source, _infer_node_type(source), name=source)
        for target in targets:
            ceg.upsert_node(target, _infer_node_type(target), name=target)
            edge_id = ceg.add_edge(source, target, "must_review", "governance", confidence=0.5)
            ceg.add_evidence(edge_id, "frontmatter", "legacy_annotation", 0.8, detail=conv.get("reason", ""))


def _load_legacy_doc_link(ceg: CEG, link: dict):
    req = link.get("requirement")
    design = link.get("design")
    code_files = link.get("code", [])
    test_files = link.get("test", [])
    db_tables = link.get("db", [])

    if req:
        ceg.upsert_node(req, "requirement", name=req)
        if design:
            ceg.upsert_node(design, "design", name=design)
            edge_id = ceg.add_edge(req, design, "specifies", "governance")
            ceg.add_evidence(edge_id, "frontmatter", "legacy_annotation", 0.9)
        for code in code_files:
            ceg.upsert_node(code, "file", path=code, name=code)
            edge_id = ceg.add_edge(req, code, "implements", "governance")
            ceg.add_evidence(edge_id, "frontmatter", "legacy_annotation", 0.9)

    for code in code_files:
        ceg.upsert_node(code, "file", path=code, name=code)
        for test in test_files:
            ceg.upsert_node(test, "test_case", path=test, name=test)
            edge_id = ceg.add_edge(code, test, "tests", "validation")
            ceg.add_evidence(edge_id, "frontmatter", "legacy_annotation", 0.85)
        for table in db_tables:
            ceg.upsert_node(table, "db_table", name=table)
            edge_id = ceg.add_edge(code, table, "writes_table", "structural")
            ceg.add_evidence(edge_id, "frontmatter", "legacy_annotation", 0.8)


def _load_legacy_data_dependency(ceg: CEG, dep: dict):
    table = dep.get("table", "")
    column = dep.get("column", "")
    node_id = f"db_column:{table}.{column}"
    ceg.upsert_node(node_id, "db_column", name=f"{table}.{column}")
    for affected in dep.get("affects", []):
        ceg.upsert_node(affected, "file", path=affected, name=affected)
        edge_id = ceg.add_edge(node_id, affected, "behavioral_dependency", "behavioral")
        ceg.add_evidence(edge_id, "frontmatter", "legacy_annotation", 0.75, detail=dep.get("condition", ""))


# ═══════════════════════════════════════════════════════════
# Phase 2: Source code scanning
# ═══════════════════════════════════════════════════════════

def _scan_source_directory(ceg: CEG, project_root: Path, src_dir: Path,
                           language: str, exclude_patterns: list):
    """Scan source files for import/call dependencies."""
    # Contract Kernel Cut Condition A: source extensions come from the
    # registry-DATA strategy (single source of truth, no language-keyed dict).
    from codd.parsing.regex_strategies import language_extensions

    exts = language_extensions(language)

    file_count = 0
    for root, dirs, files in os.walk(src_dir):
        for fname in files:
            if not any(fname.endswith(ext) for ext in exts):
                continue
            full = Path(root) / fname
            # Re-confine each walked source file (escape GATE for in-root
            # symlinks whose target leaves the tree); ``rel`` keeps its original
            # derivation so in-root nodes are byte-identical (anti-false-red).
            if resolve_project_path(project_root, full) is None:
                continue
            rel = full.relative_to(project_root).as_posix()

            if any(_match_glob(rel, pat) for pat in exclude_patterns):
                continue

            ceg.upsert_node(f"file:{rel}", "file", path=rel, name=fname)
            file_count += 1
            _extract_imports_basic(ceg, project_root, src_dir, full, rel, language)

    if file_count > 0:
        print(f"  Source: {file_count} {language} files in {src_dir.relative_to(project_root)}")


def _extract_imports_basic(ceg: CEG, project_root: Path, src_dir: Path, file_path: Path,
                           rel_path: str, language: str):
    """Basic import extraction using the shared parsing backend."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return

    source_id = f"file:{rel_path}"
    extractor = get_extractor(language, "source")
    internal, _ = extractor.extract_imports(content, file_path, project_root, src_dir)

    # Contract Kernel Cut Condition A: the per-language relative-import resolution
    # is a registry-DATA capability (see :func:`ceg_import_targets`), so this
    # scanner core builds the graph WITHOUT a ``if language in (...)`` branch.
    # Byte-identical to the former inline python / typescript-javascript block.
    from codd.parsing.regex_strategies import ceg_import_targets

    for target in ceg_import_targets(language, internal, project_root, file_path):
        ceg.upsert_node(target.target_id, target.node_type, **target.node_kwargs)
        edge_id = ceg.add_edge(source_id, target.target_id, "imports", "structural")
        ceg.add_evidence(edge_id, "static", target.evidence_method, target.confidence)


# ═══════════════════════════════════════════════════════════
# Phase 3: Filesystem route scanning
# ═══════════════════════════════════════════════════════════

def _scan_filesystem_routes(ceg: CEG, project_root: Path, fs_route_configs: list[dict]):
    """Scan framework filesystem routes and add endpoint nodes."""
    try:
        from codd.parsing import FileSystemRouteExtractor
    except ImportError:
        return

    extractor = FileSystemRouteExtractor()
    route_info = extractor.extract_routes(project_root, fs_route_configs)

    endpoint_count = 0
    for route in getattr(route_info, "routes", []):
        url = route.get("url")
        route_file = route.get("file")
        if not url or not route_file:
            continue

        rel_file = _project_relative_path(project_root, route_file)
        node_id = f"endpoint:{url}"
        ceg.upsert_node(node_id, "endpoint", path=rel_file, name=url)

        node = ceg.get_node(node_id)
        if node is not None:
            node["url"] = url
            node["endpoint_kind"] = route.get("kind", "page")
            node["source_type"] = "static"

        file_node_id = f"file:{rel_file}"
        ceg.upsert_node(file_node_id, "file", path=rel_file, name=Path(rel_file).name)
        edge_id = ceg.add_edge(file_node_id, node_id, "implements", "structural")
        ceg.add_evidence(edge_id, "static", "filesystem_route", 0.95)
        endpoint_count += 1

    print(f"  Filesystem routes: {endpoint_count} endpoint nodes generated")


# ═══════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════

def _match_glob(path: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(path, pattern)


def _project_relative_path(project_root: Path, file_path: str) -> str:
    path = Path(file_path)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def _collect_document_warnings(rel_path: str, codd_data: dict) -> list[str]:
    warnings = []
    if codd_data.get("type") == "design" and not _has_dependency_refs(codd_data.get("depends_on")):
        warnings.append(f"{rel_path}: design document has empty depends_on")
    return warnings


def _has_dependency_refs(entries) -> bool:
    if not entries:
        return False
    for entry in entries:
        if isinstance(entry, str) and entry:
            return True
        if isinstance(entry, dict) and (entry.get("id") or entry.get("node_id")):
            return True
    return False


def _collect_wave_config_warnings(project_root: Path, config: dict) -> list[str]:
    wave_config = config.get("wave_config")
    if not wave_config:
        return []

    from codd.generator import _load_wave_artifacts

    warnings = []
    for artifact in _load_wave_artifacts(config):
        output_path = project_root / artifact.output
        if not output_path.exists():
            warnings.append(
                f"{artifact.output}: wave_config defines {artifact.node_id} but the file has not been generated"
            )
    return warnings


def _infer_node_type(node_id: str) -> str:
    prefixes = {
        "db_table:": "db_table", "db_column:": "db_column",
        "module:": "module", "file:": "file", "test:": "test_case",
        "config:": "config_key", "endpoint:": "endpoint",
        "infra:": "infrastructure", "db:": "db_object",
        "req:": "requirement", "design:": "design", "doc:": "document",
    }
    for prefix, node_type in prefixes.items():
        if node_id.startswith(prefix):
            return node_type
    return "unknown"
