"""CoDD propagate — reverse-propagate source code changes to design documents."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from codd.config import load_project_config
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


def run_propagate(
    project_root: Path,
    diff_target: str = "HEAD",
    update: bool = False,
    ai_command: str | None = None,
    feedback: str | None = None,
) -> PropagationResult:
    """Detect source code changes and find affected design documents.

    When update=True, calls AI to update each affected design doc.
    """
    config = load_project_config(project_root)
    source_dirs = config.get("scan", {}).get("source_dirs", [])

    # Step 1: Get changed files
    changed_files = _get_changed_files(project_root, diff_target)
    if not changed_files:
        return PropagationResult([], {}, [], [])

    # Step 2: Filter to source files and map to modules
    file_module_map = _map_files_to_modules(changed_files, source_dirs)
    if not file_module_map:
        return PropagationResult(changed_files, {}, [], [])

    changed_modules = set(file_module_map.values())

    # Step 3: Find design docs covering those modules
    affected_docs = _find_design_docs_by_modules(
        project_root, config, changed_modules, file_module_map,
    )
    if not affected_docs:
        return PropagationResult(changed_files, file_module_map, [], [])

    # Step 4: Optionally update via AI
    updated: list[str] = []
    if update and affected_docs:
        resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="propagate")
        code_diff = _get_code_diff(project_root, diff_target, list(file_module_map.keys()))

        for doc in affected_docs:
            doc_path = project_root / doc.path
            if not doc_path.exists():
                continue
            current_content = doc_path.read_text(encoding="utf-8")
            prompt = _build_update_prompt(doc, current_content, code_diff, feedback=feedback)
            raw_body = _invoke_ai_command(resolved_ai_command, prompt)
            body = _sanitize_update_body(raw_body)

            _write_updated_doc(doc_path, current_content, body)
            updated.append(doc.node_id)

    return PropagationResult(changed_files, file_module_map, affected_docs, updated)


def _get_changed_files(project_root: Path, diff_target: str) -> list[str]:
    """Get changed files from git diff."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", diff_target],
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


def _get_code_diff(
    project_root: Path,
    diff_target: str,
    files: list[str],
) -> str:
    """Get the actual code diff for specific files."""
    try:
        result = subprocess.run(
            ["git", "diff", diff_target, "--"] + files,
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
    """Build a prompt for AI to update a design document based on code changes."""
    lines = [
        "You are UPDATING an existing design document to reflect source code changes.",
        "",
        "The code diff below shows what changed in the source code.",
        "The current design document is provided in full.",
        "Your task is to update ONLY the parts of the design document that are affected by the code changes.",
        "",
        "CRITICAL RULES:",
        "- Preserve the existing document structure and frontmatter EXACTLY.",
        "- Only modify sections that are directly affected by the code diff.",
        "- If the code change is a bug fix or minor refactoring that doesn't affect the design, output the body UNCHANGED.",
        "- Do NOT add new sections or remove existing sections.",
        "- Do NOT change the title.",
        "- Do NOT emit YAML frontmatter — only the body content.",
        "",
        f"Document: {doc.node_id}",
        f"Title: {doc.title}",
        f"Covers modules: {', '.join(doc.modules)}",
        f"Changed modules: {', '.join(doc.matched_modules)}",
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
