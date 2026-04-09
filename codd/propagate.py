"""Change propagation engine — impact analysis from git diff."""

import subprocess
from datetime import datetime
from pathlib import Path

import yaml

from codd.graph import CEG
from codd.scanner import _extract_frontmatter


def run_impact(project_root: Path, codd_dir: Path, diff_target: str,
               output_path: str = None):
    """Run impact analysis from git diff."""
    config = yaml.safe_load((codd_dir / "codd.yaml").read_text())
    scan_dir = codd_dir / "scan"
    ceg = CEG(scan_dir)

    # Get changed files from git diff
    changed_files = _get_changed_files(project_root, diff_target)
    if not changed_files:
        print("No changed files detected.")
        ceg.close()
        return

    print(f"Changed files: {len(changed_files)}")
    for f in changed_files:
        print(f"  - {f}")

    # Load band thresholds from config
    bands = config.get("bands", {})
    green_conf = bands.get("green", {}).get("min_confidence", 0.90)
    green_evidence = bands.get("green", {}).get("min_evidence_count", 2)
    amber_conf = bands.get("amber", {}).get("min_confidence", 0.50)
    max_depth = config.get("propagation", {}).get("max_depth", 10)

    # Resolve changed files to graph node IDs
    start_nodes = _resolve_start_nodes(ceg, project_root, changed_files)
    print(f"Resolved to {len(start_nodes)} graph nodes:")
    for node_id, source in start_nodes:
        print(f"  - {node_id} (from {source})")

    # Propagate impact from all start nodes
    all_impacts = {}
    for node_id, source_file in start_nodes:
        impacts = ceg.propagate_impact(node_id, max_depth=max_depth)
        for target_id, info in impacts.items():
            if target_id not in all_impacts or info["depth"] < all_impacts[target_id]["depth"]:
                all_impacts[target_id] = {
                    **info,
                    "source": source_file,
                }

    # Check conventions from graph (must_review edges)
    convention_impacts = _check_conventions_from_graph(ceg, start_nodes)

    # Generate report
    report = _generate_report(
        ceg, changed_files, start_nodes, all_impacts, convention_impacts,
        green_conf, green_evidence, amber_conf
    )

    if output_path:
        Path(output_path).write_text(report)
        print(f"Report written to {output_path}")
    else:
        print("\n" + report)

    ceg.close()


def _get_changed_files(project_root: Path, diff_target: str) -> list:
    """Get list of changed files from git diff."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--name-only", diff_target],
            capture_output=True, text=True, cwd=str(project_root)
        )
        if result.returncode != 0:
            print(f"Warning: git diff failed: {result.stderr.strip()}")
            return []
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except FileNotFoundError:
        print("Warning: git not found.")
        return []


def _resolve_start_nodes(ceg: CEG, project_root: Path, changed_files: list) -> list:
    """Resolve changed files to graph node IDs.

    A changed .md file with CoDD frontmatter resolves to its node_id (e.g. design:system-design).
    A changed source file resolves to file:path.
    Returns list of (node_id, source_file) tuples.
    """
    start_nodes = []
    seen = set()

    for changed_file in changed_files:
        full_path = project_root / changed_file

        # Check if it's a document with CoDD frontmatter
        if changed_file.endswith(".md") and full_path.exists():
            codd_data = _extract_frontmatter(full_path)
            if codd_data and "node_id" in codd_data:
                node_id = codd_data["node_id"]
                if node_id not in seen and ceg.get_node(node_id):
                    start_nodes.append((node_id, changed_file))
                    seen.add(node_id)
                continue

        # Check file:path node
        file_node_id = f"file:{changed_file}"
        if file_node_id not in seen and ceg.get_node(file_node_id):
            start_nodes.append((file_node_id, changed_file))
            seen.add(file_node_id)

        # Also check if any node has this path
        path_nodes = ceg.find_nodes_by_path(changed_file)
        for node in path_nodes:
            if node["id"] not in seen:
                start_nodes.append((node["id"], changed_file))
                seen.add(node["id"])

    return start_nodes


def _check_conventions_from_graph(ceg: CEG, start_nodes: list) -> list:
    """Check if changed nodes trigger convention (must_review) edges in the graph.

    Walks the graph looking for must_review edges reachable from start nodes.
    """
    triggered = []
    checked = set()

    for node_id, source_file in start_nodes:
        # Direct must_review edges from the changed node
        conv_edges = ceg.get_convention_edges(node_id)
        for edge in conv_edges:
            key = (node_id, edge["target_id"])
            if key in checked:
                continue
            checked.add(key)

            # Get the evidence detail (reason) for this convention
            reason = ""
            for ev in edge.get("evidence", []):
                if ev.get("detail"):
                    reason = ev["detail"]
                    break

            triggered.append({
                "source_node": node_id,
                "target_id": edge["target_id"],
                "target_name": edge["target_name"],
                "target_type": edge["target_type"],
                "reason": reason,
                "confidence": edge["confidence"],
                "triggered_by": source_file,
            })

        # Also check must_review edges from nodes that depend on the changed node
        incoming = ceg.get_incoming_edges(node_id)
        for inc_edge in incoming:
            parent_id = inc_edge["source_id"]
            parent_convs = ceg.get_convention_edges(parent_id)
            for edge in parent_convs:
                key = (parent_id, edge["target_id"])
                if key in checked:
                    continue
                checked.add(key)

                reason = ""
                for ev in edge.get("evidence", []):
                    if ev.get("detail"):
                        reason = ev["detail"]
                        break

                triggered.append({
                    "source_node": parent_id,
                    "target_id": edge["target_id"],
                    "target_name": edge["target_name"],
                    "target_type": edge["target_type"],
                    "reason": reason,
                    "confidence": edge["confidence"],
                    "triggered_by": source_file,
                })

    return triggered


def _generate_report(ceg: CEG, changed_files: list, start_nodes: list,
                     graph_impacts: dict, convention_impacts: list,
                     green_conf: float, green_evidence: int,
                     amber_conf: float) -> str:
    """Generate a Markdown impact report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# CoDD Impact Report",
        f"",
        f"**Generated**: {now}",
        f"**Changed files**: {len(changed_files)}",
        f"**Resolved nodes**: {len(start_nodes)}",
        f"",
    ]

    # Changed files → resolved nodes
    lines.append("## Changed Files")
    lines.append("")
    for f in changed_files:
        # Find matching start node
        matched = [n for n, s in start_nodes if s == f]
        if matched:
            lines.append(f"- `{f}` → `{matched[0]}`")
        else:
            lines.append(f"- `{f}` (not in graph)")
    lines.append("")

    # Convention alerts (highest priority)
    if convention_impacts:
        lines.append("## Convention Alerts")
        lines.append("")
        for ci in convention_impacts:
            lines.append(f"### must_review: `{ci['target_name']}`")
            lines.append(f"")
            lines.append(f"- **Source**: `{ci['source_node']}`")
            lines.append(f"- **Target**: `{ci['target_id']}` ({ci['target_type']})")
            lines.append(f"- **Reason**: {ci['reason']}")
            lines.append(f"- **Confidence**: {ci['confidence']:.2f}")
            lines.append(f"- **Triggered by**: `{ci['triggered_by']}`")
            lines.append("")

    # Graph-based impacts
    if graph_impacts:
        # Classify into bands
        green_items = []
        amber_items = []
        gray_items = []

        for target_id, info in graph_impacts.items():
            node = ceg.get_node(target_id)
            edges = ceg.get_incoming_edges(target_id)
            evidence_count = sum(1 for _ in edges)
            max_conf = max((e["confidence"] for e in edges), default=0.0)
            band = ceg.classify_band(max_conf, evidence_count, green_conf, green_evidence, amber_conf)

            item = {
                "id": target_id,
                "name": node["name"] if node else target_id,
                "type": node["type"] if node else "unknown",
                "depth": info["depth"],
                "confidence": max_conf,
                "source": info.get("source", "?"),
            }

            if band == "green":
                green_items.append(item)
            elif band == "amber":
                amber_items.append(item)
            else:
                gray_items.append(item)

        lines.append("## Impact Propagation")
        lines.append("")

        if green_items:
            lines.append("### Green Band (high confidence, auto-propagate)")
            lines.append("")
            lines.append("| Target | Type | Depth | Confidence | Source |")
            lines.append("|--------|------|-------|------------|--------|")
            for item in sorted(green_items, key=lambda x: x["depth"]):
                lines.append(f"| `{item['name']}` | {item['type']} | {item['depth']} | {item['confidence']:.2f} | `{item['source']}` |")
            lines.append("")

        if amber_items:
            lines.append("### Amber Band (must review)")
            lines.append("")
            lines.append("| Target | Type | Depth | Confidence | Source |")
            lines.append("|--------|------|-------|------------|--------|")
            for item in sorted(amber_items, key=lambda x: x["depth"]):
                lines.append(f"| `{item['name']}` | {item['type']} | {item['depth']} | {item['confidence']:.2f} | `{item['source']}` |")
            lines.append("")

        if gray_items:
            lines.append("### Gray Band (informational)")
            lines.append("")
            lines.append("| Target | Type | Depth | Confidence | Source |")
            lines.append("|--------|------|-------|------------|--------|")
            for item in sorted(gray_items, key=lambda x: x["depth"]):
                lines.append(f"| `{item['name']}` | {item['type']} | {item['depth']} | {item['confidence']:.2f} | `{item['source']}` |")
            lines.append("")

    if not graph_impacts and not convention_impacts:
        lines.append("## Result")
        lines.append("")
        lines.append("No impacts detected. Changed files have no tracked dependencies in the graph.")
        lines.append("")

    # Stats
    stats = ceg.stats()
    lines.append("## Graph Stats")
    lines.append("")
    lines.append(f"- Nodes: {stats['nodes']}")
    lines.append(f"- Edges: {stats['edges']}")
    lines.append(f"- Evidence records: {stats['evidence']}")
    lines.append("")

    return "\n".join(lines)
