"""CoDD Scanner — Extract dependency data from document frontmatter + source code.

Design principle: Documents ARE the data (Single Source of Truth).
Dependency metadata is embedded as YAML frontmatter in deliverable documents.
Auto-generated data (frontmatter, AST) is refreshed on scan.
Human knowledge (manual annotations, overrides) is NEVER deleted.
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml

from codd.graph import CEG
from codd.parsing import get_extractor


def run_scan(project_root: Path, codd_dir: Path):
    """Scan all project documents and source code, refresh auto-generated data.

    Human knowledge (source_type='human') is preserved.
    Auto-generated data (frontmatter, static, framework) is purged and rebuilt.
    """
    config_path = codd_dir / "codd.yaml"
    if not config_path.exists():
        print("Error: codd/codd.yaml not found.")
        raise SystemExit(1)

    config = yaml.safe_load(config_path.read_text())
    scan_dir = codd_dir / "scan"

    ceg = CEG(scan_dir)

    # Purge auto-generated data, keep human knowledge
    purged = ceg.purge_auto_generated()
    human_count = ceg.count_human_evidence()
    print(f"Purged auto-generated: {purged['evidence']} evidence, {purged['edges']} edges, {purged['nodes']} nodes")
    if human_count > 0:
        print(f"Preserved: {human_count} human evidence records")

    # Phase 1: Scan document frontmatter (all .md/.yaml in doc_dirs)
    doc_dirs = config["scan"].get("doc_dirs", [])
    frontmatter_count = 0
    warnings = []
    for doc_dir in doc_dirs:
        full_path = project_root / doc_dir
        if full_path.exists():
            count, doc_warnings = _scan_frontmatter(ceg, project_root, full_path)
            frontmatter_count += count
            warnings.extend(doc_warnings)

    # Phase 1b: Also scan codd/annotations/ if it exists (backward compat)
    annotations_dir = codd_dir / "annotations"
    if annotations_dir.exists():
        _load_legacy_annotations(ceg, annotations_dir)

    # Phase 2: Scan source code (imports, calls)
    language = config["project"].get("language", "python")
    source_dirs = config["scan"].get("source_dirs", [])
    exclude_patterns = config["scan"].get("exclude", [])

    for src_dir in source_dirs:
        full_path = project_root / src_dir
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
    try:
        content = file_path.read_text(errors="ignore")
    except Exception:
        return None

    # Match YAML frontmatter between --- delimiters
    match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return None

    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None

    if not isinstance(frontmatter, dict):
        return None

    return frontmatter.get("codd")


def build_document_node_path_map(project_root: Path, config: dict[str, Any]) -> dict[str, Path]:
    """Resolve document node IDs to project-relative paths."""
    node_paths: dict[str, Path] = {}

    for doc_dir in config.get("scan", {}).get("doc_dirs", []):
        full_path = project_root / doc_dir
        if not full_path.exists():
            continue

        for root, _, files in os.walk(full_path):
            for fname in files:
                if not fname.endswith(".md"):
                    continue

                full = Path(root) / fname
                rel = full.relative_to(project_root)
                codd_data = _extract_frontmatter(full)
                if not codd_data:
                    continue

                node_id = codd_data.get("node_id", f"doc:{rel.as_posix()}")
                node_paths[str(node_id)] = rel

    from codd.generator import _load_wave_artifacts

    try:
        artifacts = _load_wave_artifacts(config)
    except ValueError:
        artifacts = []

    for artifact in artifacts:
        node_paths.setdefault(artifact.node_id, Path(artifact.output))

    return node_paths


def _load_frontmatter(ceg: CEG, doc_path: str, codd: dict):
    """Load CoDD frontmatter data into the graph."""
    node_id = codd.get("node_id", f"doc:{doc_path}")
    node_type = codd.get("type", "document")
    ceg.upsert_node(node_id, node_type, path=doc_path, name=node_id)

    # Process depends_on (outgoing edges from this document)
    for dep in codd.get("depends_on", []):
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

    # Process depended_by (incoming edges — other things that depend on this)
    for dep in codd.get("depended_by", []):
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
        data = yaml.safe_load(conv_path.read_text())
        for conv in (data or {}).get("conventions", []):
            _load_legacy_convention(ceg, conv)
            loaded = True

    links_path = annotations_dir / "doc_links.yaml"
    if links_path.exists():
        data = yaml.safe_load(links_path.read_text())
        for link in (data or {}).get("links", []):
            _load_legacy_doc_link(ceg, link)
            loaded = True

    deps_path = annotations_dir / "data_dependencies.yaml"
    if deps_path.exists():
        data = yaml.safe_load(deps_path.read_text())
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
    extensions = {
        "python": [".py"],
        "typescript": [".ts", ".tsx"],
        "javascript": [".js", ".jsx"],
        "java": [".java"],
        "go": [".go"],
    }
    exts = extensions.get(language, [])

    file_count = 0
    for root, dirs, files in os.walk(src_dir):
        for fname in files:
            if not any(fname.endswith(ext) for ext in exts):
                continue
            full = Path(root) / fname
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
        content = file_path.read_text(errors="ignore")
    except Exception:
        return

    source_id = f"file:{rel_path}"
    extractor = get_extractor(language, "source")
    internal, _ = extractor.extract_imports(content, file_path, project_root, src_dir)

    if language in ("typescript", "javascript"):
        for import_lines in internal.values():
            for line in import_lines:
                match = re.search(r'''(?:import|from)\s+['"]([^'"]+)['"]''', line)
                if not match:
                    continue
                target_module = match.group(1)
                if not target_module.startswith("."):
                    continue
                resolved = (file_path.parent / target_module).resolve()
                extensions = [".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", "/index.ts", "/index.tsx", "/index.js", "/index.jsx"]
                for ext in [""] + extensions:
                    candidate = Path(f"{resolved}{ext}")
                    if not candidate.exists():
                        continue
                    try:
                        target_rel = candidate.relative_to(project_root).as_posix()
                    except ValueError:
                        continue
                    target_id = f"file:{target_rel}"
                    ceg.upsert_node(target_id, "file", path=target_rel)
                    edge_id = ceg.add_edge(source_id, target_id, "imports", "structural")
                    ceg.add_evidence(edge_id, "static", "ast_import", 0.95)
                    break

    elif language == "python":
        for target_module in internal:
            target_id = f"module:{target_module}"
            ceg.upsert_node(target_id, "module", name=target_module)
            edge_id = ceg.add_edge(source_id, target_id, "imports", "structural")
            ceg.add_evidence(edge_id, "static", "ast_import", 0.90)


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
