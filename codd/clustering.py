"""R4.2 — Feature clustering for codd extract.

Groups modules by functional cohesion using call graph edges,
naming conventions, and cross-reference density.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codd.extractor import ProjectFacts


def build_feature_clusters(facts: ProjectFacts) -> None:
    """Populate ``facts.feature_clusters`` by analysing call edges and naming."""
    from codd.extractor import FeatureCluster

    module_names = list(facts.modules.keys())
    if len(module_names) < 2:
        return

    # Step 1: Build adjacency from call edges
    adj: dict[str, set[str]] = defaultdict(set)
    for mod in facts.modules.values():
        for edge in mod.call_edges:
            # edge.callee may be "module.Class.method" — extract target module
            target_mod = _resolve_callee_module(edge.callee, module_names)
            if target_mod and target_mod != mod.name:
                adj[mod.name].add(target_mod)
                adj[target_mod].add(mod.name)

    # Step 2: Find connected components via call graph
    components = _connected_components(module_names, adj)

    # Step 3: Merge with naming prefix heuristics
    prefix_groups = _group_by_prefix(module_names)

    # Step 4: Combine call-graph components with prefix groups
    clusters: list[FeatureCluster] = []
    seen: set[str] = set()

    # First: call-graph components (higher confidence)
    for comp in components:
        if len(comp) < 2:
            continue
        name = _infer_cluster_name(comp)
        evidence: list[str] = []

        # Check if they share naming prefix
        common_prefix = _common_prefix(comp)
        if common_prefix:
            evidence.append(f"shared prefix: {common_prefix}")

        # Count call edges between members
        edge_count = sum(
            1 for m in comp for n in adj.get(m, set()) if n in comp
        )
        if edge_count > 0:
            evidence.append(f"{edge_count} cross-call edges")

        confidence = min(1.0, 0.4 + 0.1 * edge_count + (0.2 if common_prefix else 0.0))

        clusters.append(FeatureCluster(
            name=name,
            modules=sorted(comp),
            confidence=round(confidence, 2),
            evidence=evidence,
        ))
        seen.update(comp)

    # Second: prefix-only groups (lower confidence)
    for prefix, members in prefix_groups.items():
        remaining = [m for m in members if m not in seen]
        if len(remaining) < 2:
            continue
        clusters.append(FeatureCluster(
            name=prefix,
            modules=sorted(remaining),
            confidence=0.3,
            evidence=[f"shared prefix: {prefix}"],
        ))
        seen.update(remaining)

    facts.feature_clusters = sorted(clusters, key=lambda c: -c.confidence)


def _resolve_callee_module(callee: str, module_names: list[str]) -> str | None:
    """Map a callee like 'auth.verify_token' to module name 'auth'."""
    # Try exact match first
    if callee in module_names:
        return callee
    # Try first dotted segment
    parts = callee.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in module_names:
            return candidate
    # Try just the first part (top-level module)
    if parts[0] in module_names:
        return parts[0]
    return None


def _connected_components(nodes: list[str], adj: dict[str, set[str]]) -> list[set[str]]:
    """Find connected components in an undirected graph."""
    visited: set[str] = set()
    components: list[set[str]] = []

    for node in nodes:
        if node in visited:
            continue
        # BFS
        component: set[str] = set()
        queue = [node]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            for neighbor in adj.get(current, set()):
                if neighbor not in visited:
                    queue.append(neighbor)
        components.append(component)

    return components


def _group_by_prefix(module_names: list[str]) -> dict[str, list[str]]:
    """Group modules sharing a common naming prefix (e.g., 'auth_*')."""
    groups: dict[str, list[str]] = defaultdict(list)
    for name in module_names:
        # Split on underscore or dot
        parts = name.replace(".", "_").split("_")
        if len(parts) >= 2:
            prefix = parts[0]
            if len(prefix) >= 2:  # Avoid single-char prefixes
                groups[prefix].append(name)
    # Only return groups with 2+ members
    return {k: v for k, v in groups.items() if len(v) >= 2}


def _common_prefix(names: set[str]) -> str:
    """Find common prefix among module names, if any."""
    if not names:
        return ""
    name_list = sorted(names)
    parts_list = [n.replace(".", "_").split("_") for n in name_list]
    if not parts_list or not parts_list[0]:
        return ""
    prefix_parts: list[str] = []
    for i, part in enumerate(parts_list[0]):
        if all(len(p) > i and p[i] == part for p in parts_list):
            prefix_parts.append(part)
        else:
            break
    return "_".join(prefix_parts) if prefix_parts else ""


def _infer_cluster_name(modules: set[str]) -> str:
    """Infer a human-readable name for a cluster."""
    prefix = _common_prefix(modules)
    if prefix:
        return prefix
    # Fall back to shortest module name
    return min(modules, key=len)
