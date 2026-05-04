"""Requirements change propagation analysis."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from codd.config import find_codd_dir, load_project_config
from codd.graph import CEG
from codd.propagator import (
    AffectedDoc,
    _build_update_prompt,
    _invoke_ai_command,
    _resolve_ai_command,
    _sanitize_update_body,
    _write_updated_doc,
)


REQUIREMENT_PATH_PREFIXES = ("docs/requirements/", "requirements/")
_FRONTMATTER_FIELD = re.compile(r"^\s*([A-Za-z0-9_.-]+):\s*(.*?)\s*$")


def require_propagate(
    project_root: Path,
    base_ref: str | None = None,
    apply: bool = False,
    ai_command: str | None = None,
) -> int:
    """Detect requirement changes and propose updates for dependent design docs."""
    base_ref = base_ref or "HEAD~1"
    changes = _detect_requirements_changes(project_root, base_ref)
    if not changes:
        print(f"No requirements changes detected since {base_ref}.")
        return 0

    print(f"Requirements changes detected ({len(changes)}):")
    for change in changes:
        print(
            f"  - {change['file']}: {change['field']} "
            f"{change['old']} -> {change['new']}"
        )

    scan_dir = _find_ceg_scan_dir(project_root)
    if scan_dir is None:
        print("Warning: CoDD graph not found. Run `codd scan` first.")
        return 1

    ceg = CEG(scan_dir)
    try:
        affected_docs = _find_affected_design_docs(ceg, changes)

        if not affected_docs:
            print("No affected design docs found.")
            return 0

        print(f"Affected design docs ({len(affected_docs)}):")
        for doc in affected_docs:
            display = doc["path"] or doc["node_id"]
            print(f"  - {display} ({doc['node_id']})")
            if doc["triggered_by"]:
                print(f"    triggered_by: {', '.join(doc['triggered_by'])}")

        proposals = _generate_update_proposals(
            project_root,
            changes,
            affected_docs,
            ceg,
            ai_command=ai_command,
        )
    finally:
        ceg.close()

    if not proposals:
        print("No update proposals generated.")
        return 0

    if apply:
        return _apply_proposals(project_root, proposals)

    _display_proposals(project_root, proposals)
    print(f"\n{len(proposals)} proposal(s) generated. Use --apply to write changes.")
    return 0


def _detect_requirements_changes(project_root: Path, base_ref: str) -> list[dict]:
    """Detect frontmatter field changes under requirements doc paths."""
    from codd._git_helper import _diff_files

    diff_text = _diff_files(
        base_ref,
        cwd=project_root,
        paths=list(REQUIREMENT_PATH_PREFIXES),
    )
    return _parse_frontmatter_changes(diff_text)


def _parse_frontmatter_changes(diff_text: str) -> list[dict]:
    changes: list[dict] = []
    current_file: str | None = None
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    field_order: list[str] = []
    in_frontmatter = False
    saw_frontmatter = False

    def remember(field: str, value: str, bucket: dict[str, str]) -> None:
        if field not in field_order:
            field_order.append(field)
        bucket[field] = value

    def flush() -> None:
        nonlocal removed, added, field_order
        if current_file is None:
            return
        for field in field_order:
            old = removed.get(field)
            new = added.get(field)
            if old == new:
                continue
            changes.append(
                {
                    "file": current_file,
                    "field": field,
                    "old": old,
                    "new": new,
                }
            )
        removed = {}
        added = {}
        field_order = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            current_file = _path_from_diff_header(line)
            in_frontmatter = False
            saw_frontmatter = False
            continue

        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):].strip()
            continue
        if line.startswith("--- a/") or line.startswith("index "):
            continue
        if current_file is None or not _is_requirement_path(current_file):
            continue
        if not line or line[0] not in " +-":
            continue

        prefix = line[0]
        content = line[1:]
        if content.strip() == "---":
            if not saw_frontmatter:
                saw_frontmatter = True
                in_frontmatter = True
            elif in_frontmatter:
                in_frontmatter = False
            continue
        if not in_frontmatter:
            continue
        if prefix not in "+-":
            continue

        match = _FRONTMATTER_FIELD.match(content)
        if not match:
            continue
        field, value = match.groups()
        if prefix == "-":
            remember(field, value, removed)
        else:
            remember(field, value, added)

    flush()
    return changes


def _find_ceg_scan_dir(project_root: Path) -> Path | None:
    candidates: list[Path] = []
    codd_dir = find_codd_dir(project_root)

    if codd_dir is not None:
        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}
        graph_path = config.get("graph", {}).get("path")
        if graph_path:
            candidates.append(project_root / graph_path)
        candidates.append(codd_dir / "scan")

    candidates.extend(
        [
            project_root / ".codd" / "scan",
            project_root / "codd" / "scan",
            project_root / ".codd",
        ]
    )

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "nodes.jsonl").exists() and (candidate / "edges.jsonl").exists():
            return candidate
    return None


def _find_affected_design_docs(ceg: CEG, changes: list[dict]) -> list[dict]:
    affected: dict[str, dict] = {}

    for change in changes:
        req_file = change["file"]
        nodes = ceg.find_nodes_by_path(req_file)
        for node in nodes:
            for edge in ceg.find_depended_by(node["id"]):
                source_id = edge.get("source_id")
                if not source_id:
                    continue
                source_node = ceg.get_node(source_id) or {}
                if not _is_design_doc_node(source_node):
                    continue
                doc = affected.setdefault(
                    source_id,
                    {
                        "node_id": source_id,
                        "path": source_node.get("path"),
                        "triggered_by": [],
                    },
                )
                trigger = f"{req_file}:{change['field']}"
                if trigger not in doc["triggered_by"]:
                    doc["triggered_by"].append(trigger)

    return sorted(
        affected.values(),
        key=lambda doc: (doc["path"] or "", doc["node_id"]),
    )


def _generate_update_proposals(
    project_root: Path,
    changes: list[dict],
    affected_nodes: list[dict],
    ceg: CEG,
    ai_command: str | None = None,
) -> list[dict[str, Any]]:
    """Generate AI update proposals for affected design documents."""
    if not affected_nodes:
        return []

    config = _load_config_for_ai(project_root)
    resolved_ai_command = _resolve_ai_command(
        config,
        ai_command,
        command_name="propagate",
    )
    requirements_diff = _format_changes_for_prompt(changes)
    proposals: list[dict[str, Any]] = []

    for node in affected_nodes:
        doc_path = _resolve_node_path(node, project_root)
        if doc_path is None or not doc_path.exists():
            display = node.get("path") or node.get("node_id") or "(unknown)"
            print(f"Warning: affected design doc not found: {display}")
            continue

        current_content = doc_path.read_text(encoding="utf-8")
        affected_doc = _to_affected_doc(node, changes, ceg)
        try:
            prompt = _build_update_prompt(
                affected_doc,
                current_content,
                requirements_diff,
            )
            raw_body = _invoke_ai_command(resolved_ai_command, prompt)
            proposal_body = _sanitize_update_body(raw_body)
        except Exception as exc:
            display = _display_path(project_root, doc_path)
            print(f"Warning: proposal generation failed for {display}: {exc}")
            continue

        proposals.append(
            {
                "path": doc_path,
                "node_id": affected_doc.node_id,
                "proposal": proposal_body,
                "original": current_content,
                "triggered_by": list(node.get("triggered_by", [])),
            }
        )

    return proposals


def _apply_proposals(project_root: Path, proposals: list[dict[str, Any]]) -> int:
    """Apply generated update proposal bodies to their design documents."""
    if not proposals:
        print("No update proposals to apply.")
        return 0

    applied = 0
    for proposal in proposals:
        doc_path = Path(proposal["path"])
        try:
            _write_updated_doc(
                doc_path,
                str(proposal["original"]),
                str(proposal["proposal"]),
            )
        except Exception as exc:
            print(f"Warning: apply failed for {_display_path(project_root, doc_path)}: {exc}")
            continue

        applied += 1
        print(f"Applied proposal to {_display_path(project_root, doc_path)}.")

    print(f"{applied} proposal(s) applied.")
    return 0 if applied == len(proposals) else 1


def _display_proposals(project_root: Path, proposals: list[dict[str, Any]]) -> None:
    """Display unified diffs for generated update proposals without writing files."""
    for proposal in proposals:
        doc_path = Path(proposal["path"])
        rel_path = _display_path(project_root, doc_path)
        original = str(proposal["original"])
        updated = _render_updated_doc_content(original, str(proposal["proposal"]))
        diff_lines = list(
            difflib.unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
            )
        )

        print(f"\nProposal for {rel_path}:")
        if not diff_lines:
            print("  (no changes proposed)")
            continue
        for line in diff_lines:
            print(line)


def _format_changes_for_prompt(changes: list[dict]) -> str:
    """Format requirement frontmatter changes for the propagation prompt."""
    if not changes:
        return "No requirements frontmatter changes were detected."

    lines = [
        "Requirements frontmatter changes detected:",
        "",
    ]
    for change in changes:
        file_path = change.get("file") or "(unknown file)"
        field = change.get("field") or "(unknown field)"
        old_value = _format_prompt_value(change.get("old"))
        new_value = _format_prompt_value(change.get("new"))
        lines.append(
            f"- {file_path}: {field} changed from {old_value} to {new_value}"
        )
    return "\n".join(lines)


def _render_updated_doc_content(original_content: str, new_body: str) -> str:
    """Render the document content that _write_updated_doc would write."""
    match = re.match(r"^(---\s*\n.*?\n---\s*\n)", original_content, re.DOTALL)
    frontmatter = match.group(1) if match else ""

    body_lines = new_body.strip().split("\n")
    if body_lines and body_lines[0].startswith("# "):
        title_match = re.search(r"^# .+$", original_content, re.MULTILINE)
        if title_match:
            body_lines[0] = title_match.group(0)

    return frontmatter + "\n".join(body_lines) + "\n"


def _to_affected_doc(node: dict, changes: list[dict], ceg: CEG) -> AffectedDoc:
    node_id = node.get("node_id") or node.get("id") or ""
    source_node = ceg.get_node(node_id) or {}
    path = node.get("path") or source_node.get("path") or ""
    modules = _coerce_str_list(source_node.get("modules"))
    module = source_node.get("module")
    if module and module not in modules:
        modules.append(str(module))

    return AffectedDoc(
        node_id=node_id,
        path=path,
        title=str(source_node.get("title") or source_node.get("name") or _title_from_path(path)),
        modules=modules,
        matched_modules=[],
        changed_files=_unique_change_files(changes),
    )


def _resolve_node_path(node: dict, project_root: Path) -> Path | None:
    path = node.get("path")
    if not path:
        return None
    doc_path = Path(path)
    if doc_path.is_absolute():
        return doc_path
    return project_root / doc_path


def _load_config_for_ai(project_root: Path) -> dict:
    try:
        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return {}


def _unique_change_files(changes: list[dict]) -> list[str]:
    files: list[str] = []
    for change in changes:
        file_path = change.get("file")
        if file_path and file_path not in files:
            files.append(file_path)
    return files


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _format_prompt_value(value: Any) -> str:
    if value is None:
        return "(missing)"
    return repr(str(value))


def _title_from_path(path: str) -> str:
    if not path:
        return "(untitled)"
    return Path(path).stem.replace("_", " ").replace("-", " ").title()


def _display_path(project_root: Path, doc_path: Path) -> str:
    try:
        return doc_path.relative_to(project_root).as_posix()
    except ValueError:
        return doc_path.as_posix()


def _is_requirement_path(path: str) -> bool:
    normalized = path.lstrip("./")
    return any(normalized.startswith(prefix) for prefix in REQUIREMENT_PATH_PREFIXES)


def _is_design_doc_node(node: dict) -> bool:
    path = node.get("path") or ""
    node_type = node.get("type")
    if node_type == "design":
        return True
    return path.endswith(".md") and not _is_requirement_path(path)


def _path_from_diff_header(line: str) -> str | None:
    parts = line.split()
    if len(parts) >= 4 and parts[3].startswith("b/"):
        return parts[3][2:]
    return None
