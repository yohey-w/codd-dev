"""Requirements change propagation analysis."""

from __future__ import annotations

import re
from pathlib import Path

from codd.config import find_codd_dir, load_project_config
from codd.graph import CEG


REQUIREMENT_PATH_PREFIXES = ("docs/requirements/", "requirements/")
_FRONTMATTER_FIELD = re.compile(r"^\s*([A-Za-z0-9_.-]+):\s*(.*?)\s*$")


def require_propagate(project_root: Path, base_ref: str | None = None) -> int:
    """Detect changed requirement frontmatter and list dependent design docs."""
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
    finally:
        ceg.close()

    if not affected_docs:
        print("No affected design docs found.")
        return 0

    print(f"Affected design docs ({len(affected_docs)}):")
    for doc in affected_docs:
        display = doc["path"] or doc["node_id"]
        print(f"  - {display} ({doc['node_id']})")
        if doc["triggered_by"]:
            print(f"    triggered_by: {', '.join(doc['triggered_by'])}")
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
