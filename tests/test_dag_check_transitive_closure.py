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


def test_empty_dag_pass():
    result = _run(_dag())

    assert result.unreachable_nodes == []
    assert result.passed is True


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
