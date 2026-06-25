"""Transitive closure DAG completeness check."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag.checks import register_dag_check


@dataclass
class TransitiveClosureResult:
    check_name: str = "transitive_closure"
    severity: str = "amber"
    unreachable_nodes: list[str] = field(default_factory=list)
    common_node_count: int = 0
    passed: bool = True
    # ``status``/``skipped``/``checked_count`` close a vacuous-pass hole: an empty
    # DAG (or one whose every node is ``common`` and therefore exempt from
    # reachability) used to return a green PASS having examined zero nodes.
    # ``checked_count`` is the number of non-common nodes whose reachability was
    # actually evaluated; when it is 0 the run is a SKIP, not a clean pass.
    status: str = "pass"
    skipped: bool = False
    checked_count: int = 0


# Edge kinds that represent an in-project source-import dependency
# (importer -> imported). Used to identify *code-entry roots*: source nodes that
# nothing in the project imports. This is a structural classification of edges,
# not a language/framework branch.
_IMPORT_EDGE_KINDS = frozenset({"imports"})

# Node kinds that are NOT first-class source nodes for entry-root detection:
# design docs are handled by their own root rule, and ``common`` nodes are shared
# infrastructure exempt from reachability. Anything else (``impl_file`` and any
# future source kind) is treated as a source node — no per-language literal.
_NON_SOURCE_KINDS = frozenset({"design_doc", "common"})


@register_dag_check("transitive_closure")
class TransitiveClosureCheck:
    """Report unreachable nodes without blocking deploy.

    A node is *reachable* if it can be reached from **any entry root**:

    - **design_doc roots** — ``design_doc`` nodes with no incoming edge (the
      classic "every artifact descends from a design document" model); and
    - **code-entry roots** — source nodes (everything that is neither a
      ``design_doc`` nor a ``common`` node) with no incoming in-project *import*
      edge, i.e. the package's public entry points (e.g. a package
      ``__init__`` or a CLI ``__main__`` that nothing else imports).

    Seeding from the *union* of these makes reachability meaningful on doc-less
    BROWNFIELD projects (raw external code with zero design docs but a fully
    connected import graph) while leaving doc-rooted projects unchanged:
    code-entry roots are purely additive. Only nodes unreachable from ALL entries
    are flagged — genuine orphans.

    Entry-root detection is structural (incoming-edge analysis on import-kind
    edges), never a ``language ==`` branch.

    Cycle / no-entry fallback: if a project has source nodes but none qualify as
    a code-entry root (every source node has an in-project importer — a pure
    cycle), reachability falls back to seeding from *all* source nodes for that
    project, so connectivity is still measured rather than reported as a false
    "everything unreachable". Cycles are traversed safely (visited-set guard).

    Nodes with ``kind == "common"`` (shared infrastructure declared via
    ``common_node_patterns`` or frontmatter ``node_type: common``) remain exempt
    from unreachable detection. They participate in the DAG so change-impact
    analysis still sees them, but they do not need to be the descendant of a
    single design document.
    """

    def __init__(self, dag=None, project_root: Path | None = None, settings: dict[str, Any] | None = None):
        self.dag = dag
        self.project_root = project_root
        self.settings = settings or {}

    def run(
        self,
        dag=None,
        project_root: Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> TransitiveClosureResult:
        dag = dag or self.dag
        if dag is None or not dag.nodes:
            # No nodes at all → reachability examined nothing → SKIP (not a
            # vacuous green PASS).
            return TransitiveClosureResult(
                status="skip",
                skipped=True,
                checked_count=0,
            )

        to_ids = {edge.to_id for edge in dag.edges}
        design_roots = [
            node.id
            for node in dag.nodes.values()
            if node.kind == "design_doc" and node.id not in to_ids
        ]
        code_roots = self._code_entry_roots(dag)

        # Reachable from ANY entry: design-doc roots OR code-entry roots. The
        # union keeps doc-rooted projects unchanged (design roots still seed) and
        # makes doc-less brownfield projects measurable (code roots seed the
        # connected impl graph).
        roots = list(dict.fromkeys([*design_roots, *code_roots]))

        visited = self._reachable_from(dag, roots)
        common_count = sum(
            1 for node in dag.nodes.values() if node.kind == "common"
        )
        # Common nodes are exempt from unreachable detection, so the number of
        # nodes whose reachability is actually evaluated is the non-common set.
        checked = sum(1 for node in dag.nodes.values() if node.kind != "common")
        unreachable = [
            node_id
            for node_id, node in dag.nodes.items()
            if node_id not in visited and node.kind != "common"
        ]

        if checked == 0:
            # Every node is exempt (all ``common``): reachability examined nothing.
            # A clean PASS here is a vacuous false-green → SKIP instead.
            return TransitiveClosureResult(
                unreachable_nodes=[],
                common_node_count=common_count,
                passed=True,
                status="skip",
                skipped=True,
                checked_count=0,
            )

        return TransitiveClosureResult(
            unreachable_nodes=unreachable,
            common_node_count=common_count,
            passed=True,
            status="pass",
            skipped=False,
            checked_count=checked,
        )

    def _code_entry_roots(self, dag) -> list[str]:
        """Public entry points of the import graph (heads nothing imports).

        A *source node* is any node that is neither a ``design_doc`` (handled by
        its own root rule) nor a ``common`` node (exempt infrastructure). A source
        node is a *code-entry root* when it **participates in the import graph as a
        head**: it has at least one outgoing in-project import edge (it imports
        something) AND no incoming import edge (nothing imports it). These are the
        package's public entry points (e.g. ``__init__`` / ``__main__``).

        Requiring an *outgoing* import edge is what preserves no-false-green: a
        node with zero import edges (isolated -- imported by nothing and importing
        nothing) is a genuine orphan, NOT an entry root, and therefore stays
        flagged. Only nodes that actually anchor an import chain seed reachability.

        Fallback (pure cycle / no head): if no source node qualifies as a head but
        some source nodes still *participate* in the import graph (a cycle where
        every node has an importer), seed from all participating source nodes so
        connectivity is still measured instead of a false "everything
        unreachable". Isolated orphans (no import edges) are never seeded and stay
        flagged even in the fallback.
        """
        source_ids = {
            node.id
            for node in dag.nodes.values()
            if node.kind not in _NON_SOURCE_KINDS
        }
        if not source_ids:
            return []

        import_to: set[str] = set()
        import_from: set[str] = set()
        for edge in dag.edges:
            if edge.kind in _IMPORT_EDGE_KINDS:
                import_to.add(edge.to_id)
                import_from.add(edge.from_id)

        # Heads: import something in-project, imported by nothing in-project.
        entry_roots = [
            nid
            for nid in source_ids
            if nid in import_from and nid not in import_to
        ]
        if entry_roots:
            return entry_roots

        # No clean head (pure cycle): seed from source nodes that participate in
        # the import graph at all, so connectivity is still measured. Nodes with
        # no import edges remain genuine orphans (never seeded here).
        return [nid for nid in source_ids if nid in import_to or nid in import_from]

    def _reachable_from(self, dag, roots: list[str]) -> set[str]:
        visited: set[str] = set()
        queue = deque(roots)

        while queue:
            current = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            for edge in dag.edges:
                if edge.from_id == current and edge.to_id not in visited:
                    queue.append(edge.to_id)

        return visited
