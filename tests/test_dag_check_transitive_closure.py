from codd.dag import DAG, Edge, Node
from codd.dag.checks import get_registry
from codd.dag.checks.transitive_closure import (
    TransitiveClosureCheck,
    TransitiveClosureResult,
)


def _dag() -> DAG:
    return DAG()


def _node(dag: DAG, node_id: str, kind: str = "design_doc") -> None:
    dag.add_node(Node(id=node_id, kind=kind, path=node_id))


def _edge(dag: DAG, from_id: str, to_id: str, kind: str = "expects") -> None:
    dag.add_edge(Edge(from_id=from_id, to_id=to_id, kind=kind))


def _run(dag: DAG) -> TransitiveClosureResult:
    return TransitiveClosureCheck().run(dag, None, {})


def test_transitive_closure_registered():
    assert get_registry()["transitive_closure"] is TransitiveClosureCheck


def test_fully_connected_dag_no_unreachable():
    dag = _dag()
    _node(dag, "docs/design/system.md")
    _node(dag, "src/app.ts", "impl_file")
    _edge(dag, "docs/design/system.md", "src/app.ts")

    result = _run(dag)

    assert result.unreachable_nodes == []


def test_isolated_node_reported():
    dag = _dag()
    _node(dag, "docs/design/system.md")
    _node(dag, "src/app.ts", "impl_file")
    _node(dag, "src/orphan.ts", "impl_file")
    _edge(dag, "docs/design/system.md", "src/app.ts")

    result = _run(dag)

    assert result.unreachable_nodes == ["src/orphan.ts"]


def test_multiple_unreachable_collected():
    dag = _dag()
    _node(dag, "docs/design/system.md")
    _node(dag, "src/app.ts", "impl_file")
    _node(dag, "src/orphan.ts", "impl_file")
    _node(dag, "docs/design/orphan.md")
    _edge(dag, "docs/design/system.md", "src/app.ts")
    _edge(dag, "src/orphan.ts", "docs/design/orphan.md", "references")

    result = _run(dag)

    assert result.unreachable_nodes == ["src/orphan.ts", "docs/design/orphan.md"]


def test_severity_is_amber():
    assert _run(_dag()).severity == "amber"


def test_passed_always_true():
    dag = _dag()
    _node(dag, "src/orphan.ts", "impl_file")

    result = _run(dag)

    assert result.unreachable_nodes == ["src/orphan.ts"]
    assert result.passed is True
    # One non-common node WAS examined (and found unreachable), so this is a real
    # finding, not a no-input skip.
    assert result.skipped is False
    assert result.checked_count == 1


def test_empty_dag_skips():
    """Empty DAG = no nodes to check reachability for → SKIP, not vacuous PASS."""
    from codd.dag.materiality import is_vacuous_pass

    result = _run(_dag())

    assert result.unreachable_nodes == []
    assert result.passed is True
    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0
    assert is_vacuous_pass(result) is False


def test_all_common_nodes_skips():
    """A DAG of only ``common`` nodes exempts every node from reachability.

    Zero nodes are actually examined for reachability, so it must SKIP rather
    than return a green PASS that verified nothing (vacuous false-green).
    """
    from codd.dag.materiality import is_vacuous_pass

    dag = _dag()
    _node(dag, "src/shared.ts", "common")
    _node(dag, "src/util.ts", "common")

    result = _run(dag)

    assert result.unreachable_nodes == []
    assert result.passed is True
    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0
    assert result.common_node_count == 2
    assert is_vacuous_pass(result) is False


def test_reachable_pass_counts_checked_nodes():
    """A real reachability verification reports status='pass' + checked_count>0."""
    from codd.dag.materiality import is_vacuous_pass

    dag = _dag()
    _node(dag, "docs/design/system.md")
    _node(dag, "src/app.ts", "impl_file")
    _edge(dag, "docs/design/system.md", "src/app.ts")

    result = _run(dag)

    assert result.unreachable_nodes == []
    assert result.passed is True
    assert result.status == "pass"
    assert result.skipped is False
    assert result.checked_count == 2  # both non-common nodes examined
    assert is_vacuous_pass(result) is False


def test_single_root_traversal():
    dag = _dag()
    _node(dag, "docs/design/system.md")
    _node(dag, "docs/design/api.md")
    _node(dag, "src/api.ts", "impl_file")
    _edge(dag, "docs/design/system.md", "docs/design/api.md", "depends_on")
    _edge(dag, "docs/design/api.md", "src/api.ts")

    result = _run(dag)

    assert result.unreachable_nodes == []


def test_multiple_roots_traversal():
    dag = _dag()
    _node(dag, "docs/design/system.md")
    _node(dag, "docs/design/admin.md")
    _node(dag, "src/app.ts", "impl_file")
    _node(dag, "src/admin.ts", "impl_file")
    _edge(dag, "docs/design/system.md", "src/app.ts")
    _edge(dag, "docs/design/admin.md", "src/admin.ts")

    result = _run(dag)

    assert result.unreachable_nodes == []


def test_cycle_handling():
    dag = _dag()
    _node(dag, "docs/design/system.md")
    _node(dag, "docs/design/api.md")
    _node(dag, "src/api.ts", "impl_file")
    _edge(dag, "docs/design/system.md", "docs/design/api.md", "depends_on")
    _edge(dag, "docs/design/api.md", "docs/design/api.md", "depends_on")
    _edge(dag, "docs/design/api.md", "src/api.ts")

    result = _run(dag)

    assert result.unreachable_nodes == []


def test_result_fields_present():
    result = _run(_dag())

    assert result.check_name == "transitive_closure"
    assert result.severity == "amber"
    assert result.unreachable_nodes == []
    assert result.passed is True


def test_deploy_non_blocking():
    dag = _dag()
    _node(dag, "docs/design/system.md")
    _node(dag, "src/orphan.ts", "impl_file")

    result = _run(dag)

    assert result.unreachable_nodes == ["src/orphan.ts"]
    assert result.passed is True
    assert result.severity == "amber"


# --- Brownfield (doc-less) reachability ------------------------------------
#
# A raw external codebase (e.g. Flask) has ZERO ``design_doc`` nodes but a fully
# connected impl graph wired by ``imports`` edges. Seeding reachability only from
# ``design_doc`` roots reports EVERYTHING unreachable on exactly these brownfield
# targets. Reachability must additionally seed from *code-entry roots*: impl
# source nodes with no incoming in-project ``imports`` edge (the package's public
# entry points). Only nodes unreachable from ALL entries are genuine orphans.


def _import(dag: DAG, importer: str, imported: str) -> None:
    """An ``imports`` edge: importer -> imported (importer depends on imported)."""
    _edge(dag, importer, imported, "imports")


def test_brownfield_no_design_docs_reachable_from_code_entry_root():
    """Doc-less project: a component connected from a code-entry root is reachable.

    ``__init__.py`` is imported by nothing in-project (the public entry point);
    it imports ``app.py`` which imports ``helpers.py`` — a connected component.
    ``orphan.py`` is imported by nothing AND imports nothing (a true orphan).

    Pre-fix: roots are design_doc-only → ``[]`` roots → every node unreachable
    (the connected component is falsely flagged). Post-fix: the connected
    component is reachable from the ``__init__.py`` code-entry root; only the
    true orphan is flagged.
    """
    dag = _dag()
    _node(dag, "pkg/__init__.py", "impl_file")
    _node(dag, "pkg/app.py", "impl_file")
    _node(dag, "pkg/helpers.py", "impl_file")
    _node(dag, "pkg/orphan.py", "impl_file")
    # __init__ -> app -> helpers (connected component, __init__ is the entry)
    _import(dag, "pkg/__init__.py", "pkg/app.py")
    _import(dag, "pkg/app.py", "pkg/helpers.py")
    # orphan: no edges in or out -> genuinely unreachable

    result = _run(dag)

    assert result.unreachable_nodes == ["pkg/orphan.py"]
    assert "pkg/__init__.py" not in result.unreachable_nodes
    assert "pkg/app.py" not in result.unreachable_nodes
    assert "pkg/helpers.py" not in result.unreachable_nodes
    # All four impl nodes were examined for reachability.
    assert result.checked_count == 4
    assert result.passed is True


def test_brownfield_pure_cycle_falls_back_to_all_nodes():
    """No clear entry (every impl node has an in-project importer / a cycle).

    a -> b -> c -> a: every node has an incoming import edge, so there is no
    code-entry root. Rather than report a false "all unreachable", fall back to
    seeding from the impl nodes so connectivity is still measured. Must not crash
    on the cycle.
    """
    dag = _dag()
    _node(dag, "pkg/a.py", "impl_file")
    _node(dag, "pkg/b.py", "impl_file")
    _node(dag, "pkg/c.py", "impl_file")
    _import(dag, "pkg/a.py", "pkg/b.py")
    _import(dag, "pkg/b.py", "pkg/c.py")
    _import(dag, "pkg/c.py", "pkg/a.py")  # cycle closes -> no node lacks an importer

    result = _run(dag)

    assert result.unreachable_nodes == []
    assert result.checked_count == 3
    assert result.passed is True


def test_doc_rooted_project_unchanged_by_code_entry_seeding():
    """Generality guard: a project WITH design docs is unchanged.

    The impl file is reachable via the design_doc root (as before). The code-entry
    roots are purely additive and must not change the outcome here, and an
    unreferenced impl node is STILL flagged (no false-green).
    """
    dag = _dag()
    _node(dag, "docs/design/system.md")  # design_doc root
    _node(dag, "src/app.py", "impl_file")
    _node(dag, "src/orphan.py", "impl_file")  # referenced by nothing -> orphan
    _edge(dag, "docs/design/system.md", "src/app.py")  # expects edge

    result = _run(dag)

    # app reachable from the doc root; orphan still flagged.
    assert result.unreachable_nodes == ["src/orphan.py"]
    assert result.passed is True
