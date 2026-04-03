"""CoDD measure — project metrics for change management effectiveness.

Collects and reports metrics that demonstrate CoDD's value:
- Document coverage: how much of the project is tracked by CoDD
- Graph health: connectivity, orphan nodes, circular dependencies
- Policy compliance: violation trends over time
- Change impact stats: average propagation depth, affected nodes per change

These metrics help stakeholders understand:
1. How well the project is managed (coverage, health)
2. How effective CoDD is at catching issues (policy, validation)
3. What the change risk profile looks like (impact stats)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from codd.config import find_codd_dir, load_project_config
from codd.policy import run_policy
from codd.validator import validate_project


@dataclass
class GraphMetrics:
    """Metrics about the dependency graph."""

    total_nodes: int = 0
    total_edges: int = 0
    orphan_nodes: int = 0  # Nodes with no incoming or outgoing edges
    max_depth: int = 0  # Longest path in the graph
    avg_out_degree: float = 0.0  # Average number of dependencies per node

    @property
    def connectivity(self) -> float:
        """Ratio of actual edges to maximum possible edges (0-1)."""
        if self.total_nodes < 2:
            return 0.0
        max_edges = self.total_nodes * (self.total_nodes - 1)
        return min(self.total_edges / max_edges, 1.0)


@dataclass
class CoverageMetrics:
    """Metrics about document coverage."""

    tracked_files: int = 0  # Files referenced in CoDD documents
    source_files: int = 0  # Total source files in configured dirs
    design_documents: int = 0  # Number of CoDD design docs

    @property
    def coverage_ratio(self) -> float:
        """Fraction of source files covered by design docs (0-1)."""
        if self.source_files == 0:
            return 0.0
        return min(self.tracked_files / self.source_files, 1.0)


@dataclass
class QualityMetrics:
    """Metrics about validation and policy compliance."""

    validation_errors: int = 0
    validation_warnings: int = 0
    policy_critical: int = 0
    policy_warnings: int = 0
    documents_checked: int = 0
    files_policy_checked: int = 0
    rules_applied: int = 0


@dataclass
class MeasureResult:
    """Aggregate metrics for a CoDD project."""

    graph: GraphMetrics = field(default_factory=GraphMetrics)
    coverage: CoverageMetrics = field(default_factory=CoverageMetrics)
    quality: QualityMetrics = field(default_factory=QualityMetrics)

    @property
    def health_score(self) -> int:
        """Overall health score (0-100). Higher is better."""
        score = 100

        # Deductions for validation issues
        score -= self.quality.validation_errors * 10
        score -= self.quality.validation_warnings * 2

        # Deductions for policy violations
        score -= self.quality.policy_critical * 15
        score -= self.quality.policy_warnings * 3

        # Deductions for orphan nodes (poor graph connectivity)
        if self.graph.total_nodes > 0:
            orphan_ratio = self.graph.orphan_nodes / self.graph.total_nodes
            score -= int(orphan_ratio * 20)

        # Bonus for coverage
        if self.coverage.coverage_ratio > 0.8:
            score = min(score + 5, 100)

        return max(score, 0)


def run_measure(project_root: Path) -> MeasureResult:
    """Collect all metrics for a CoDD project."""
    project_root = project_root.resolve()
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        raise FileNotFoundError("CoDD config dir not found. Run 'codd init' first.")

    config = load_project_config(project_root)
    result = MeasureResult()

    # Graph metrics
    result.graph = _collect_graph_metrics(codd_dir)

    # Coverage metrics
    result.coverage = _collect_coverage_metrics(project_root, codd_dir, config)

    # Quality metrics
    result.quality = _collect_quality_metrics(project_root, codd_dir)

    return result


def _collect_graph_metrics(codd_dir: Path) -> GraphMetrics:
    """Collect metrics from the dependency graph."""
    scan_dir = codd_dir / "scan"
    if not scan_dir.exists():
        return GraphMetrics()

    from codd.graph import CEG

    ceg = CEG(scan_dir)
    try:
        nodes = list(ceg.nodes())
        total_nodes = len(nodes)
        total_edges = 0
        out_degrees: list[int] = []
        in_degree: dict[str, int] = {n: 0 for n in nodes}

        for node_id in nodes:
            deps = list(ceg.dependencies(node_id))
            out_degrees.append(len(deps))
            total_edges += len(deps)
            for dep_id, _ in deps:
                if dep_id in in_degree:
                    in_degree[dep_id] += 1

        # Orphans: no in-edges AND no out-edges
        orphan_count = sum(
            1 for n in nodes
            if in_degree.get(n, 0) == 0 and (out_degrees[nodes.index(n)] if n in nodes else 0) == 0
        )

        avg_out = sum(out_degrees) / total_nodes if total_nodes > 0 else 0.0

        # Max depth via BFS from all root nodes
        max_depth = _compute_max_depth(ceg, nodes, in_degree)

    finally:
        ceg.close()

    return GraphMetrics(
        total_nodes=total_nodes,
        total_edges=total_edges,
        orphan_nodes=orphan_count,
        max_depth=max_depth,
        avg_out_degree=round(avg_out, 2),
    )


def _compute_max_depth(ceg, nodes: list[str], in_degree: dict[str, int]) -> int:
    """Compute the longest path in the dependency graph."""
    roots = [n for n in nodes if in_degree.get(n, 0) == 0]
    if not roots:
        return 0

    max_depth = 0
    for root in roots:
        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(root, 0)]
        while queue:
            node, depth = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            max_depth = max(max_depth, depth)
            for dep_id, _ in ceg.dependencies(node):
                if dep_id not in visited:
                    queue.append((dep_id, depth + 1))

    return max_depth


def _collect_coverage_metrics(
    project_root: Path, codd_dir: Path, config: dict
) -> CoverageMetrics:
    """Collect coverage metrics: how many source files are tracked by CoDD."""
    source_dirs = (config.get("scan") or {}).get("source_dirs", [])
    exclude_patterns = (config.get("scan") or {}).get("exclude", [])

    # Count source files
    source_files = 0
    for src_dir in source_dirs:
        full_path = project_root / src_dir
        if full_path.exists():
            source_files += sum(1 for f in full_path.rglob("*") if f.is_file())

    # Count design documents
    doc_dirs = (config.get("scan") or {}).get("doc_dirs", [])
    design_docs = 0
    tracked_files: set[str] = set()

    for doc_dir in doc_dirs:
        full_path = project_root / doc_dir
        if not full_path.exists():
            continue
        for f in full_path.rglob("*.md"):
            if f.is_file():
                design_docs += 1
                # Parse frontmatter for source_refs
                _extract_source_refs(f, tracked_files)

    return CoverageMetrics(
        tracked_files=len(tracked_files),
        source_files=source_files,
        design_documents=design_docs,
    )


def _extract_source_refs(doc_path: Path, tracked: set[str]) -> None:
    """Extract source file references from a design document's frontmatter."""
    try:
        content = doc_path.read_text(errors="ignore")
    except OSError:
        return

    # Simple frontmatter parsing — look for source_refs in YAML frontmatter
    if not content.startswith("---"):
        return

    end = content.find("---", 3)
    if end < 0:
        return

    import yaml

    try:
        fm = yaml.safe_load(content[3:end])
    except yaml.YAMLError:
        return

    if not isinstance(fm, dict):
        return

    refs = fm.get("source_refs", [])
    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, str):
                tracked.add(ref)


def _collect_quality_metrics(project_root: Path, codd_dir: Path) -> QualityMetrics:
    """Collect validation and policy metrics."""
    # Validation
    validation = validate_project(project_root, codd_dir)

    # Policy
    policy = run_policy(project_root)

    return QualityMetrics(
        validation_errors=validation.error_count,
        validation_warnings=validation.warning_count,
        policy_critical=policy.critical_count,
        policy_warnings=policy.warning_count,
        documents_checked=validation.documents_checked,
        files_policy_checked=policy.files_checked,
        rules_applied=policy.rules_applied,
    )


def format_measure_text(result: MeasureResult) -> str:
    """Format metrics as human-readable text."""
    lines: list[str] = []
    lines.append(f"CoDD Project Metrics — Health Score: {result.health_score}/100")
    lines.append("")

    # Graph
    g = result.graph
    lines.append(f"Graph:   {g.total_nodes} nodes, {g.total_edges} edges, "
                 f"{g.orphan_nodes} orphans, max depth {g.max_depth}")
    lines.append(f"         avg out-degree {g.avg_out_degree}, "
                 f"connectivity {g.connectivity:.3f}")

    # Coverage
    c = result.coverage
    pct = f"{c.coverage_ratio:.0%}" if c.source_files > 0 else "N/A"
    lines.append(f"Coverage: {c.tracked_files}/{c.source_files} source files tracked ({pct}), "
                 f"{c.design_documents} design docs")

    # Quality
    q = result.quality
    lines.append(f"Quality: {q.documents_checked} docs validated "
                 f"({q.validation_errors} errors, {q.validation_warnings} warnings)")
    lines.append(f"         {q.files_policy_checked} files policy-checked "
                 f"({q.policy_critical} critical, {q.policy_warnings} warnings), "
                 f"{q.rules_applied} rules")

    return "\n".join(lines)


def format_measure_json(result: MeasureResult) -> str:
    """Format metrics as JSON."""
    data = {
        "health_score": result.health_score,
        "graph": asdict(result.graph),
        "coverage": {
            **asdict(result.coverage),
            "coverage_ratio": round(result.coverage.coverage_ratio, 3),
        },
        "quality": asdict(result.quality),
    }
    data["graph"]["connectivity"] = round(result.graph.connectivity, 4)
    return json.dumps(data, ensure_ascii=False, indent=2)
