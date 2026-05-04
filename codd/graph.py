"""CEG (Conditioned Evidence Graph) — JSONL file-backed dependency graph.

Design: All data lives in JSONL files (one record per line).
Files are loaded into memory on init, flushed to disk on close().
Git-friendly: every change is a line-level diff.
"""

import json
from pathlib import Path
from typing import Optional


class CEG:
    """Conditioned Evidence Graph — JSONL-backed dependency graph.

    Storage:
      {scan_dir}/nodes.jsonl  — one JSON object per line
      {scan_dir}/edges.jsonl  — one JSON object per line
    """

    def __init__(self, scan_dir: Path):
        self.scan_dir = Path(scan_dir)
        self.scan_dir.mkdir(parents=True, exist_ok=True)

        self.nodes_path = self.scan_dir / "nodes.jsonl"
        self.edges_path = self.scan_dir / "edges.jsonl"

        # In-memory stores
        self.nodes: dict[str, dict] = {}   # keyed by node_id
        self.edges: list[dict] = []         # list of edge dicts
        self._next_edge_id = 1
        self._dirty = False

        # Load existing data
        self._load()

    def _load(self):
        """Load JSONL files into memory."""
        if self.nodes_path.exists():
            for line in self.nodes_path.read_text().splitlines():
                line = line.strip()
                if line:
                    node = json.loads(line)
                    self.nodes[node["id"]] = node

        if self.edges_path.exists():
            for line in self.edges_path.read_text().splitlines():
                line = line.strip()
                if line:
                    edge = json.loads(line)
                    self.edges.append(edge)
                    if edge.get("id", 0) >= self._next_edge_id:
                        self._next_edge_id = edge["id"] + 1

    def close(self):
        """Flush to disk."""
        if self._dirty:
            self._flush()

    def _flush(self):
        """Write all data back to JSONL files."""
        # Sort nodes by id for stable output
        sorted_nodes = sorted(self.nodes.values(), key=lambda n: n["id"])
        with open(self.nodes_path, "w") as f:
            for node in sorted_nodes:
                f.write(json.dumps(node, ensure_ascii=False) + "\n")

        # Sort edges by id for stable output
        sorted_edges = sorted(self.edges, key=lambda e: e.get("id", 0))
        with open(self.edges_path, "w") as f:
            for edge in sorted_edges:
                f.write(json.dumps(edge, ensure_ascii=False) + "\n")

        self._dirty = False

    # ── Node operations ──

    def upsert_node(self, node_id: str, node_type: str, path: str = None,
                    name: str = None, module: str = None):
        node = self.nodes.get(node_id, {"id": node_id})
        node["type"] = node_type
        if path is not None:
            node["path"] = path
        if name is not None:
            node["name"] = name
        if module is not None:
            node["module"] = module
        self.nodes[node_id] = node
        self._dirty = True

    def get_node(self, node_id: str) -> Optional[dict]:
        return self.nodes.get(node_id)

    def count_nodes(self) -> int:
        return len(self.nodes)

    def find_nodes_by_path(self, path: str) -> list:
        return [n for n in self.nodes.values() if n.get("path") == path]

    def get_convention_edges(self, node_id: str) -> list:
        results = []
        for e in self.edges:
            if e["source_id"] == node_id and e["relation"] == "must_review" and e.get("is_active", True):
                target = self.nodes.get(e["target_id"], {})
                result = {**e, "target_name": target.get("name"), "target_type": target.get("type")}
                results.append(result)
        results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        return results

    # ── Edge operations ──

    def add_edge(self, source_id: str, target_id: str, relation: str,
                 semantic: str, confidence: float = 0.5,
                 condition: str = None) -> int:
        edge_id = self._next_edge_id
        self._next_edge_id += 1
        edge = {
            "id": edge_id,
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "semantic": semantic,
            "confidence": confidence,
            "is_active": True,
            "evidence": [],
        }
        if condition:
            edge["condition"] = condition
        self.edges.append(edge)
        self._dirty = True
        return edge_id

    def get_outgoing_edges(self, node_id: str, min_confidence: float = 0.0) -> list:
        results = []
        for e in self.edges:
            if (e["source_id"] == node_id and e.get("is_active", True)
                    and e.get("confidence", 0) >= min_confidence):
                target = self.nodes.get(e["target_id"], {})
                result = {**e, "target_name": target.get("name"), "target_type": target.get("type")}
                results.append(result)
        results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        return results

    def get_incoming_edges(self, node_id: str, min_confidence: float = 0.0) -> list:
        results = []
        for e in self.edges:
            if (e["target_id"] == node_id and e.get("is_active", True)
                    and e.get("confidence", 0) >= min_confidence):
                source = self.nodes.get(e["source_id"], {})
                result = {**e, "source_name": source.get("name"), "source_type": source.get("type")}
                results.append(result)
        results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        return results

    def find_depended_by(
        self,
        node_id: str,
        visited: set[str] | None = None,
        min_confidence: float = 0.0,
    ) -> list[dict]:
        """Return active depends_on edges whose sources depend on node_id.

        Edge semantics: source depends_on target. This reverse traversal starts
        from target and walks incoming depends_on edges. The visited set prevents
        cycles from recursing forever and suppresses already-seen dependers.
        """
        if visited is None:
            visited = set()
        if node_id in visited:
            return []
        visited.add(node_id)

        results: list[dict] = []
        for edge in self.get_incoming_edges(node_id, min_confidence=min_confidence):
            if edge.get("relation") != "depends_on":
                continue
            depender_id = edge.get("source_id")
            if not depender_id or depender_id in visited:
                continue

            source = self.nodes.get(depender_id, {})
            results.append({**edge, "source_path": source.get("path")})
            results.extend(
                self.find_depended_by(
                    depender_id,
                    visited=visited,
                    min_confidence=min_confidence,
                )
            )
        return results

    def count_edges(self) -> int:
        return sum(1 for e in self.edges if e.get("is_active", True))

    # ── Evidence operations ──

    def add_evidence(self, edge_id: int, source_type: str, method: str,
                     score: float, detail: str = None, is_negative: bool = False) -> int:
        for edge in self.edges:
            if edge["id"] == edge_id:
                ev = {"source_type": source_type, "method": method, "score": score}
                if detail:
                    ev["detail"] = detail
                if is_negative:
                    ev["is_negative"] = True
                edge.setdefault("evidence", []).append(ev)
                # Recalculate confidence via Noisy-OR
                edge["confidence"] = self._noisy_or(edge["evidence"])
                self._dirty = True
                return len(edge["evidence"])
        return 0

    @staticmethod
    def _noisy_or(evidence: list) -> float:
        """Noisy-OR: P(at least one fires) = 1 - product(1 - p_i)."""
        positive_product = 1.0
        negative_product = 1.0
        for ev in evidence:
            if ev.get("is_negative"):
                negative_product *= (1.0 - ev["score"])
            else:
                positive_product *= (1.0 - ev["score"])
        return round(max(0.0, (1.0 - positive_product) - (1.0 - negative_product)), 4)

    # ── Propagation ──

    def propagate_impact(self, start_node_id: str, max_depth: int = 10,
                         min_confidence: float = 0.0) -> dict:
        """BFS propagation from a changed node.

        Traces REVERSE direction: finds nodes that depend ON the changed node.
        Edge semantics: source depends_on target (source → target).
        When target changes, source is impacted. So follow incoming edges.
        """
        visited = {}
        queue = [(start_node_id, 0, [start_node_id])]

        while queue:
            current, depth, path = queue.pop(0)
            if depth > max_depth:
                continue
            if current in visited:
                continue
            visited[current] = {"depth": depth, "path": path}

            for edge in self.get_incoming_edges(current, min_confidence):
                dependent = edge["source_id"]
                if dependent not in visited:
                    queue.append((dependent, depth + 1, path + [dependent]))

        if start_node_id in visited:
            del visited[start_node_id]
        return visited

    # ── Band classification ──

    def classify_band(self, confidence: float, evidence_count: int,
                      green_threshold: float = 0.90,
                      green_min_evidence: int = 2,
                      amber_threshold: float = 0.50) -> str:
        if confidence >= green_threshold and evidence_count >= green_min_evidence:
            return "green"
        elif confidence >= amber_threshold:
            return "amber"
        else:
            return "gray"

    # ── Selective refresh ──

    AUTO_SOURCE_TYPES = ("static", "framework", "frontmatter", "inferred")
    HUMAN_SOURCE_TYPES = ("human", "dynamic", "history")

    def purge_auto_generated(self) -> dict:
        """Delete auto-generated evidence/edges/nodes, preserve human knowledge."""
        deleted_evidence = 0
        deleted_edges = 0

        # Remove auto evidence from edges
        surviving_edges = []
        for edge in self.edges:
            original_count = len(edge.get("evidence", []))
            edge["evidence"] = [
                ev for ev in edge.get("evidence", [])
                if ev.get("source_type") not in self.AUTO_SOURCE_TYPES
            ]
            deleted_evidence += original_count - len(edge["evidence"])

            if edge["evidence"]:
                edge["confidence"] = self._noisy_or(edge["evidence"])
                surviving_edges.append(edge)
            else:
                deleted_edges += 1

        self.edges = surviving_edges

        # Remove orphan nodes
        referenced = set()
        for edge in self.edges:
            referenced.add(edge["source_id"])
            referenced.add(edge["target_id"])

        orphans = [nid for nid in self.nodes if nid not in referenced]
        for nid in orphans:
            del self.nodes[nid]

        self._dirty = True
        return {
            "evidence": deleted_evidence,
            "edges": deleted_edges,
            "nodes": len(orphans),
        }

    def count_human_evidence(self) -> int:
        count = 0
        for edge in self.edges:
            for ev in edge.get("evidence", []):
                if ev.get("source_type") in self.HUMAN_SOURCE_TYPES:
                    count += 1
        return count

    # ── Stats ──

    def stats(self) -> dict:
        total_evidence = sum(len(e.get("evidence", [])) for e in self.edges)
        return {
            "nodes": self.count_nodes(),
            "edges": self.count_edges(),
            "evidence": total_evidence,
            "human_evidence": self.count_human_evidence(),
        }
