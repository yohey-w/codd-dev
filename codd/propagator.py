"""CoDD propagate — reverse-propagate source code changes to design documents."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from codd.config import find_codd_dir, load_project_config
from codd.generator import (
    _invoke_ai_command,
    _resolve_ai_command,
    MARKDOWN_FENCE_RE,
)
from codd.scanner import _extract_frontmatter


@dataclass
class AffectedDoc:
    """A design document affected by source code changes."""

    node_id: str
    path: str  # relative to project root
    title: str
    modules: list[str]
    matched_modules: list[str]  # modules that triggered the match
    changed_files: list[str]  # source files that changed in those modules


@dataclass
class PropagationResult:
    """Result of propagate analysis."""

    changed_files: list[str]
    file_module_map: dict[str, str]  # file -> module name
    affected_docs: list[AffectedDoc]
    updated: list[str]  # node_ids of docs actually updated


@dataclass
class VerifiedDoc:
    """A design doc with verification band classification."""

    doc: AffectedDoc
    band: str  # "green", "amber", "gray"
    confidence: float
    evidence_count: int


@dataclass
class VerifyResult:
    """Result of propagate --verify."""

    changed_files: list[str]
    file_module_map: dict[str, str]
    auto_applied: list[VerifiedDoc]  # green band — AI updated
    needs_hitl: list[VerifiedDoc]  # amber/gray — waiting for human
    updated: list[str]  # node_ids of auto-applied docs


@dataclass
class CommitResult:
    """Result of propagate --commit."""

    committed_files: list[str]
    knowledge_recorded: int  # number of HITL evidence entries added


# Path for verify state persistence (relative to codd dir)
VERIFY_STATE_FILE = "propagate_state.json"


def run_propagate(
    project_root: Path,
    diff_target: str = "HEAD",
    update: bool = False,
    ai_command: str | None = None,
    feedback: str | None = None,
) -> PropagationResult:
    """Detect source code changes and find affected design documents.

    When update=True, calls AI to update each affected design doc.
    Supports two propagation paths:
      1. Source code change → design docs (via module mapping)
      2. Design doc change → dependent design docs (via CEG graph)
    """
    config = load_project_config(project_root)
    source_dirs = config.get("scan", {}).get("source_dirs", [])

    # Step 1: Get changed files
    changed_files = _get_changed_files(project_root, diff_target)
    if not changed_files:
        return PropagationResult([], {}, [], [])

    # Step 2: Filter to source files and map to modules
    file_module_map = _map_files_to_modules(changed_files, source_dirs)

    affected_docs: list[AffectedDoc] = []
    diff_text = ""

    if file_module_map:
        # Path A: source code → design docs (existing logic)
        changed_modules = set(file_module_map.values())
        affected_docs = _find_design_docs_by_modules(
            project_root, config, changed_modules, file_module_map,
        )
        diff_text = _get_code_diff(project_root, diff_target, list(file_module_map.keys()))

    if not affected_docs:
        # Path B: design doc → dependent design docs (via graph)
        changed_docs = _find_changed_docs(project_root, config, changed_files)
        if changed_docs:
            affected_docs = _find_docs_depending_on(
                project_root, config, changed_docs,
            )
            diff_text = _get_code_diff(project_root, diff_target, changed_files)

    if not affected_docs:
        return PropagationResult(changed_files, file_module_map, [], [])

    # Step 4: Optionally update via AI
    updated: list[str] = []
    if update and affected_docs:
        resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="propagate")

        for doc in affected_docs:
            doc_path = project_root / doc.path
            if not doc_path.exists():
                continue
            current_content = doc_path.read_text(encoding="utf-8")
            prompt = _build_update_prompt(doc, current_content, diff_text, feedback=feedback)
            raw_body = _invoke_ai_command(resolved_ai_command, prompt)
            body = _sanitize_update_body(raw_body)

            _write_updated_doc(doc_path, current_content, body)
            updated.append(doc.node_id)

    return PropagationResult(changed_files, file_module_map, affected_docs, updated)


def run_verify(
    project_root: Path,
    diff_target: str = "HEAD",
    ai_command: str | None = None,
    feedback: str | None = None,
) -> VerifyResult:
    """Verify propagation: auto-apply green band, return HITL list for amber/gray.

    1. Detect affected docs (same as run_propagate)
    2. Load graph, classify each doc by confidence band
    3. Green band → auto-update via AI
    4. Amber/Gray → return as HITL candidates
    5. Save state for subsequent --commit
    """
    config = load_project_config(project_root)
    source_dirs = config.get("scan", {}).get("source_dirs", [])
    bands_config = config.get("bands", {})

    # Step 1: Get affected docs (source→doc or doc→doc)
    changed_files = _get_changed_files(project_root, diff_target)
    if not changed_files:
        return VerifyResult([], {}, [], [], [])

    file_module_map = _map_files_to_modules(changed_files, source_dirs)

    affected_docs: list[AffectedDoc] = []
    diff_text = ""

    if file_module_map:
        # Path A: source code → design docs
        changed_modules = set(file_module_map.values())
        affected_docs = _find_design_docs_by_modules(
            project_root, config, changed_modules, file_module_map,
        )
        diff_text = _get_code_diff(project_root, diff_target, list(file_module_map.keys()))

    if not affected_docs:
        # Path B: design doc → dependent design docs (via graph)
        changed_docs = _find_changed_docs(project_root, config, changed_files)
        if changed_docs:
            affected_docs = _find_docs_depending_on(
                project_root, config, changed_docs,
            )
            diff_text = _get_code_diff(project_root, diff_target, changed_files)

    if not affected_docs:
        return VerifyResult(changed_files, file_module_map, [], [], [])

    # Step 2: Classify by confidence band
    verified = _classify_docs_by_band(project_root, config, affected_docs, bands_config)

    auto_apply = [v for v in verified if v.band == "green"]
    hitl = [v for v in verified if v.band != "green"]

    # Step 3: Auto-apply green band via AI
    updated: list[str] = []
    if auto_apply:
        resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="propagate")

        for vdoc in auto_apply:
            doc = vdoc.doc
            doc_path = project_root / doc.path
            if not doc_path.exists():
                continue
            current_content = doc_path.read_text(encoding="utf-8")
            prompt = _build_update_prompt(doc, current_content, diff_text, feedback=feedback)
            raw_body = _invoke_ai_command(resolved_ai_command, prompt)
            body = _sanitize_update_body(raw_body)
            _write_updated_doc(doc_path, current_content, body)
            updated.append(doc.node_id)

    # Step 4: Save state for --commit
    _save_verify_state(project_root, auto_apply, hitl, updated, diff_target)

    return VerifyResult(changed_files, file_module_map, auto_apply, hitl, updated)


def run_commit(
    project_root: Path,
    reason: str | None = None,
    reason_map: dict[str, str] | None = None,
) -> CommitResult:
    """Commit after HITL review: record knowledge and git commit.

    Args:
        reason: Default reason for all HITL corrections.
        reason_map: Per-file reasons as {path: reason}. Overrides default.

    1. Load verify state (saved by --verify)
    2. Detect which HITL docs were modified by human
    3. Record HITL corrections as evidence (source_type="human")
    4. Git commit all propagation changes
    """
    state = _load_verify_state(project_root)
    if state is None:
        raise ValueError(
            "No verify state found. Run 'codd propagate --verify' first."
        )

    hitl_node_ids = set(state.get("hitl_node_ids", []))
    auto_node_ids = set(state.get("auto_node_ids", []))
    diff_target = state.get("diff_target", "HEAD")

    # Find which HITL docs were actually modified
    modified_files = _get_changed_files(project_root, diff_target)
    hitl_paths = {item["path"] for item in state.get("hitl_docs", [])}
    committed_files = [f for f in modified_files if f in hitl_paths]

    # Build per-file reason map (reason_map overrides default reason)
    effective_reasons: dict[str, str] = {}
    if reason:
        for f in committed_files:
            effective_reasons[f] = reason
    if reason_map:
        for f in committed_files:
            if f in reason_map:
                effective_reasons[f] = reason_map[f]

    # Record HITL knowledge in graph
    config = load_project_config(project_root)
    knowledge_count = 0
    if committed_files and effective_reasons:
        knowledge_count = _record_hitl_knowledge(
            project_root, state, committed_files, effective_reasons, config=config,
        )

    # Git commit
    all_paths = []
    for item in state.get("auto_docs", []) + state.get("hitl_docs", []):
        path = item["path"]
        full = project_root / path
        if full.exists():
            all_paths.append(path)

    commit_reason = _format_commit_reason(effective_reasons, reason)
    if all_paths:
        _git_commit_propagation(project_root, all_paths, commit_reason)

    # Clean up state file
    _clear_verify_state(project_root)

    return CommitResult(committed_files=committed_files, knowledge_recorded=knowledge_count)


def _format_commit_reason(
    effective_reasons: dict[str, str],
    default_reason: str | None,
) -> str | None:
    """Format commit message from per-file reasons."""
    if not effective_reasons:
        return default_reason

    # If all reasons are the same, use a single line
    unique_reasons = set(effective_reasons.values())
    if len(unique_reasons) == 1:
        return next(iter(unique_reasons))

    # Multiple reasons → per-file listing
    lines = []
    for path, r in sorted(effective_reasons.items()):
        lines.append(f"- {path}: {r}")
    return "\n".join(lines)


def _classify_docs_by_band(
    project_root: Path,
    config: dict[str, Any],
    affected_docs: list[AffectedDoc],
    bands_config: dict[str, Any],
) -> list[VerifiedDoc]:
    """Classify affected docs into green/amber/gray bands using graph evidence."""
    from codd.graph import CEG

    green_cfg = bands_config.get("green", {})
    green_threshold = green_cfg.get("min_confidence", 0.90)
    green_min_evidence = green_cfg.get("min_evidence_count", 2)
    amber_threshold = bands_config.get("amber", {}).get("min_confidence", 0.50)

    # Try to load graph for confidence data
    graph = _load_graph(project_root, config)

    verified: list[VerifiedDoc] = []
    for doc in affected_docs:
        confidence, evidence_count = _get_doc_confidence(graph, doc)

        # Use CEG.classify_band if graph available, else inline fallback
        if graph is not None:
            band = graph.classify_band(
                confidence, evidence_count,
                green_threshold, green_min_evidence, amber_threshold,
            )
        else:
            band = "amber"  # no graph → always HITL

        verified.append(VerifiedDoc(
            doc=doc, band=band,
            confidence=confidence, evidence_count=evidence_count,
        ))

    return verified


def _load_graph(project_root: Path, config: dict[str, Any]):
    """Load CEG graph if it exists, return None otherwise."""
    from codd.graph import CEG

    graph_path = config.get("graph", {}).get("path", "codd/scan")
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return None

    scan_dir = project_root / graph_path
    if not scan_dir.exists() or not (scan_dir / "nodes.jsonl").exists():
        return None

    return CEG(scan_dir)


def _get_doc_confidence(graph, doc: AffectedDoc) -> tuple[float, int]:
    """Get aggregated confidence for a doc from graph edges.

    Looks at ALL edges involving the doc's node_id (both incoming and
    outgoing) and aggregates confidence. The presence and quality of
    evidence on any edge indicates how well-established the doc is.
    Returns (confidence, evidence_count). Falls back to (0.5, 0) if no graph.
    """
    if graph is None:
        return (0.5, 0)

    node = graph.get_node(doc.node_id)
    if node is None:
        return (0.5, 0)

    # Collect all edges involving this node
    all_edges = graph.get_incoming_edges(doc.node_id) + graph.get_outgoing_edges(doc.node_id)
    if not all_edges:
        return (0.5, 0)

    # Aggregate: max confidence across all edges, sum evidence
    max_confidence = 0.0
    total_evidence = 0
    for edge in all_edges:
        conf = edge.get("confidence", 0.0)
        ev_count = len(edge.get("evidence", []))
        if conf > max_confidence:
            max_confidence = conf
        total_evidence += ev_count

    return (max_confidence, total_evidence)


def _save_verify_state(
    project_root: Path,
    auto_applied: list[VerifiedDoc],
    hitl: list[VerifiedDoc],
    updated: list[str],
    diff_target: str,
) -> None:
    """Save verify state for subsequent --commit."""
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return

    state = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "diff_target": diff_target,
        "auto_node_ids": updated,
        "hitl_node_ids": [v.doc.node_id for v in hitl],
        "auto_docs": [
            {"node_id": v.doc.node_id, "path": v.doc.path, "band": v.band,
             "confidence": v.confidence}
            for v in auto_applied
        ],
        "hitl_docs": [
            {"node_id": v.doc.node_id, "path": v.doc.path, "band": v.band,
             "confidence": v.confidence}
            for v in hitl
        ],
    }

    state_path = codd_dir / VERIFY_STATE_FILE
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_verify_state(project_root: Path) -> dict | None:
    """Load verify state from previous --verify run."""
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return None

    state_path = codd_dir / VERIFY_STATE_FILE
    if not state_path.exists():
        return None

    return json.loads(state_path.read_text(encoding="utf-8"))


def _clear_verify_state(project_root: Path) -> None:
    """Remove verify state file after commit."""
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return

    state_path = codd_dir / VERIFY_STATE_FILE
    if state_path.exists():
        state_path.unlink()


def _record_hitl_knowledge(
    project_root: Path,
    state: dict,
    committed_files: list[str],
    reasons: dict[str, str],
    config: dict[str, Any] | None = None,
) -> int:
    """Record HITL corrections as human evidence in the graph.

    Args:
        reasons: Per-file reason map {path: reason}.

    This evidence survives purge_auto_generated() and improves future
    confidence scores via Noisy-OR aggregation.
    """
    if config is None:
        config = load_project_config(project_root)
    graph = _load_graph(project_root, config)
    if graph is None:
        return 0

    count = 0
    committed_set = set(committed_files)
    hitl_docs = state.get("hitl_docs", [])

    for item in hitl_docs:
        path = item["path"]
        if path not in committed_set:
            continue

        reason = reasons.get(path, "HITL correction (no reason provided)")
        node_id = item["node_id"]
        # Find all edges involving this node and add human evidence
        all_edges = graph.get_incoming_edges(node_id) + graph.get_outgoing_edges(node_id)
        for edge in all_edges:
            graph.add_evidence(
                edge_id=edge["id"],
                source_type="human",
                method="hitl_correction",
                score=0.85,
                detail=f"HITL: {reason}",
            )
            count += 1

    graph.close()
    return count


def _git_commit_propagation(
    project_root: Path,
    files: list[str],
    reason: str | None,
) -> None:
    """Git add and commit propagation changes."""
    import logging

    logger = logging.getLogger("codd.propagator")
    try:
        add_result = subprocess.run(
            ["git", "add"] + files,
            cwd=str(project_root), capture_output=True, text=True,
        )
        if add_result.returncode != 0:
            logger.warning("git add failed: %s", add_result.stderr.strip())
            return

        msg = "docs: propagate design changes"
        if reason:
            msg += f"\n\n{reason}"
        commit_result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(project_root), capture_output=True, text=True,
        )
        if commit_result.returncode != 0:
            logger.warning("git commit failed: %s", commit_result.stderr.strip())
    except FileNotFoundError:
        pass  # git not available


def _get_changed_files(project_root: Path, diff_target: str) -> list[str]:
    """Get changed files from git diff."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--name-only", diff_target],
            capture_output=True, text=True, cwd=str(project_root),
        )
        if result.returncode != 0:
            return []
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except FileNotFoundError:
        return []


def _map_files_to_modules(
    changed_files: list[str],
    source_dirs: list[str],
) -> dict[str, str]:
    """Map changed source files to module names.

    Module = first directory under a source_dir.
    e.g. src/auth/service.py with source_dirs=["src"] → module "auth"
    """
    file_module: dict[str, str] = {}
    normalized_dirs = [d.rstrip("/") for d in source_dirs]

    for f in changed_files:
        parts = PurePosixPath(f).parts
        for src_dir in normalized_dirs:
            src_parts = PurePosixPath(src_dir).parts
            if parts[: len(src_parts)] == src_parts and len(parts) > len(src_parts) + 1:
                # First dir after source_dir is the module
                module_name = parts[len(src_parts)]
                file_module[f] = module_name
                break

    return file_module


def _find_design_docs_by_modules(
    project_root: Path,
    config: dict[str, Any],
    changed_modules: set[str],
    file_module_map: dict[str, str],
) -> list[AffectedDoc]:
    """Find design documents whose `modules` field overlaps with changed modules."""
    affected: list[AffectedDoc] = []
    seen_node_ids: set[str] = set()

    doc_dirs = config.get("scan", {}).get("doc_dirs", [])

    for doc_dir in doc_dirs:
        full_path = project_root / doc_dir
        if not full_path.exists():
            continue

        for md_file in full_path.rglob("*.md"):
            codd_data = _extract_frontmatter(md_file)
            if not codd_data or "node_id" not in codd_data:
                continue

            doc_modules = codd_data.get("modules", [])
            if not doc_modules:
                continue

            matched = set(doc_modules) & changed_modules
            if not matched:
                continue

            node_id = codd_data["node_id"]
            if node_id in seen_node_ids:
                continue
            seen_node_ids.add(node_id)

            # Collect changed files for matched modules
            matched_files = [f for f, m in file_module_map.items() if m in matched]

            rel_path = md_file.relative_to(project_root).as_posix()
            affected.append(AffectedDoc(
                node_id=node_id,
                path=rel_path,
                title=codd_data.get("title", node_id),
                modules=doc_modules,
                matched_modules=sorted(matched),
                changed_files=matched_files,
            ))

    # Also check wave_config artifacts (they have modules but may not be scanned yet)
    wave_config = config.get("wave_config")
    if wave_config:
        for _wave, artifacts in wave_config.items():
            for art in artifacts:
                modules = art.get("modules", [])
                matched = set(modules) & changed_modules
                if not matched:
                    continue
                node_id = art["node_id"]
                if node_id in seen_node_ids:
                    continue
                output_path = project_root / art["output"]
                if not output_path.exists():
                    continue
                seen_node_ids.add(node_id)
                matched_files = [f for f, m in file_module_map.items() if m in matched]
                affected.append(AffectedDoc(
                    node_id=node_id,
                    path=art["output"],
                    title=art.get("title", node_id),
                    modules=modules,
                    matched_modules=sorted(matched),
                    changed_files=matched_files,
                ))

    return affected


def _find_changed_docs(
    project_root: Path,
    config: dict[str, Any],
    changed_files: list[str],
) -> list[dict]:
    """Identify changed files that are CoDD design documents (have frontmatter).

    Returns a list of dicts with node_id, path for each changed design doc.
    """
    changed_docs: list[dict] = []
    doc_dirs = config.get("scan", {}).get("doc_dirs", [])

    for f in changed_files:
        # Check if the file is under a doc_dir and has frontmatter
        md_path = project_root / f
        if not md_path.exists() or not f.endswith(".md"):
            continue

        # Verify it's under a recognized doc_dir
        in_doc_dir = any(f.startswith(d.rstrip("/") + "/") for d in doc_dirs)
        if not in_doc_dir:
            continue

        codd_data = _extract_frontmatter(md_path)
        if codd_data and "node_id" in codd_data:
            changed_docs.append({
                "node_id": codd_data["node_id"],
                "path": f,
                "title": codd_data.get("title", codd_data["node_id"]),
            })

    return changed_docs


def _find_docs_depending_on(
    project_root: Path,
    config: dict[str, Any],
    changed_docs: list[dict],
) -> list[AffectedDoc]:
    """Find design documents that depend on the changed design documents.

    Uses the CEG graph: for each changed doc, find all docs that have an edge
    pointing TO the changed doc (incoming edges = docs that depend on it).
    """
    graph = _load_graph(project_root, config)
    if graph is None:
        return []

    changed_node_ids = {d["node_id"] for d in changed_docs}
    changed_paths = {d["path"] for d in changed_docs}
    dependent_node_ids: dict[str, set[str]] = {}  # node_id → set of triggering node_ids

    for changed_doc in changed_docs:
        # Find docs that depend on this changed doc
        incoming = graph.get_incoming_edges(changed_doc["node_id"])
        for edge in incoming:
            source_id = edge["source_id"]
            # Skip self-references and skip docs that are themselves changed
            if source_id in changed_node_ids:
                continue
            if source_id not in dependent_node_ids:
                dependent_node_ids[source_id] = set()
            dependent_node_ids[source_id].add(changed_doc["node_id"])

    if not dependent_node_ids:
        return []

    # Resolve node_ids to file paths by scanning doc_dirs
    affected: list[AffectedDoc] = []
    doc_dirs = config.get("scan", {}).get("doc_dirs", [])

    for doc_dir in doc_dirs:
        full_path = project_root / doc_dir
        if not full_path.exists():
            continue

        for md_file in full_path.rglob("*.md"):
            codd_data = _extract_frontmatter(md_file)
            if not codd_data or "node_id" not in codd_data:
                continue

            node_id = codd_data["node_id"]
            if node_id not in dependent_node_ids:
                continue

            rel_path = md_file.relative_to(project_root).as_posix()
            if rel_path in changed_paths:
                continue  # Don't update the doc that was changed

            triggering = sorted(dependent_node_ids[node_id])
            affected.append(AffectedDoc(
                node_id=node_id,
                path=rel_path,
                title=codd_data.get("title", node_id),
                modules=codd_data.get("modules", []),
                matched_modules=[],  # not module-based
                changed_files=[d["path"] for d in changed_docs
                               if d["node_id"] in dependent_node_ids[node_id]],
            ))

    return affected


def _get_code_diff(
    project_root: Path,
    diff_target: str,
    files: list[str],
) -> str:
    """Get the actual code diff for specific files."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", diff_target, "--"] + files,
            capture_output=True, text=True, cwd=str(project_root),
        )
        if result.returncode != 0:
            return ""
        return result.stdout
    except FileNotFoundError:
        return ""


def _build_update_prompt(
    doc: AffectedDoc,
    current_content: str,
    code_diff: str,
    feedback: str | None = None,
) -> str:
    """Build a prompt for AI to update a design document based on changes."""
    # Detect whether the diff is from source code or design docs
    is_doc_change = all(f.endswith(".md") for f in doc.changed_files) if doc.changed_files else False

    if is_doc_change:
        intro = [
            "You are UPDATING an existing design document to reflect changes in an upstream design document.",
            "",
            "The diff below shows what changed in the upstream design document that this document depends on.",
            "The current design document is provided in full.",
            "Your task is to update ONLY the parts that reference, quote, or depend on values from the changed upstream document.",
            "For example, if the upstream document changed a threshold from 100 to 200, update any reference to that threshold.",
        ]
    else:
        intro = [
            "You are UPDATING an existing design document to reflect source code changes.",
            "",
            "The code diff below shows what changed in the source code.",
            "The current design document is provided in full.",
            "Your task is to update ONLY the parts of the design document that are affected by the code changes.",
        ]

    lines = intro + [
        "",
        "CRITICAL RULES:",
        "- Preserve the existing document structure and frontmatter EXACTLY.",
        "- Only modify sections that are directly affected by the diff.",
        "- If the change doesn't affect this document's content, output the body UNCHANGED.",
        "- Do NOT add new sections or remove existing sections.",
        "- Do NOT change the title.",
        "- Do NOT emit YAML frontmatter — only the body content.",
        "- Do NOT prepend analysis comments or preamble before the document body.",
        "  Start directly with the first heading (# Title). No 'The code diff is...' lines.",
        "",
        f"Document: {doc.node_id}",
        f"Title: {doc.title}",
        f"Covers modules: {', '.join(doc.modules)}" if doc.modules else "",
        f"Changed modules: {', '.join(doc.matched_modules)}" if doc.matched_modules else "",
        f"Changed files: {', '.join(doc.changed_files)}",
        "",
        "--- CURRENT DESIGN DOCUMENT ---",
        current_content.rstrip(),
        "--- END CURRENT DOCUMENT ---",
        "",
        "--- CODE DIFF ---",
        code_diff[:8000] if code_diff else "(no diff available)",
        "--- END CODE DIFF ---",
        "",
    ]

    if feedback:
        lines.extend([
            "",
            "--- REVIEW FEEDBACK (from previous update attempt) ---",
            "A reviewer found issues with a previous version of this update.",
            "You MUST address ALL of the following feedback:",
            feedback.rstrip(),
            "--- END REVIEW FEEDBACK ---",
        ])

    lines.append(
        "Output the updated document body now. If no design-level changes are needed, "
        "output the existing body unchanged. Start with the first section heading.",
    )
    return "\n".join(lines).rstrip() + "\n"


def _sanitize_update_body(body: str) -> str:
    """Light sanitization for update bodies — no structural validation."""
    import re

    normalized = body.lstrip()
    # Strip accidental frontmatter
    if normalized.startswith("---"):
        match = re.match(r"^---\s*\n.*?\n---\s*\n?", normalized, re.DOTALL)
        if match:
            normalized = normalized[match.end():]
    # Strip markdown fences
    fenced = MARKDOWN_FENCE_RE.match(normalized)
    if fenced:
        normalized = fenced.group("body")
    # Strip AI preamble before the first heading.
    # Some models emit analysis comments like "The code diff is..." before
    # the actual document body.  Drop everything before the first "# " heading.
    heading_match = re.search(r"^(# .+)$", normalized, re.MULTILINE)
    if heading_match and heading_match.start() > 0:
        normalized = normalized[heading_match.start():]
    if not normalized.strip():
        raise ValueError("AI command returned empty output for propagation update")
    return normalized.strip() + "\n"


def _write_updated_doc(doc_path: Path, original_content: str, new_body: str) -> None:
    """Replace the body of a design document while preserving frontmatter."""
    # Split original into frontmatter + body
    import re

    match = re.match(r'^(---\s*\n.*?\n---\s*\n)', original_content, re.DOTALL)
    if match:
        frontmatter = match.group(1)
    else:
        frontmatter = ""

    # Find the title in the new body and skip it (frontmatter already has context)
    body_lines = new_body.strip().split("\n")
    if body_lines and body_lines[0].startswith("# "):
        # Keep the title from original
        title_match = re.search(r'^# .+$', original_content, re.MULTILINE)
        if title_match:
            title_line = title_match.group(0)
            body_lines[0] = title_line

    doc_path.write_text(frontmatter + "\n".join(body_lines) + "\n", encoding="utf-8")
