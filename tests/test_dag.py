import pytest

from codd.dag import DAG, Edge, Node
from codd.dag import checks as dag_checks


def test_dag_add_node_unique():
    dag = DAG()
    dag.add_node(Node(id="design", kind="design_doc"))

    with pytest.raises(ValueError, match="duplicate DAG node id"):
        dag.add_node(Node(id="design", kind="impl_file"))


def test_dag_add_edge():
    dag = DAG()
    dag.add_node(Node(id="design", kind="design_doc"))
    dag.add_node(Node(id="impl", kind="impl_file"))
    edge = Edge(from_id="design", to_id="impl", kind="expects")

    dag.add_edge(edge)

    assert dag.edges == [edge]


def test_dag_detect_cycles_acyclic():
    dag = DAG()
    dag.add_node(Node(id="a", kind="design_doc"))
    dag.add_node(Node(id="b", kind="impl_file"))
    dag.add_edge(Edge(from_id="a", to_id="b", kind="expects"))

    assert dag.detect_cycles() == []


def test_dag_detect_cycles_simple_cycle():
    dag = DAG()
    dag.add_node(Node(id="a", kind="design_doc"))
    dag.add_node(Node(id="b", kind="design_doc"))
    dag.add_edge(Edge(from_id="a", to_id="b", kind="depends_on"))
    dag.add_edge(Edge(from_id="b", to_id="a", kind="depends_on"))

    assert dag.detect_cycles() == [["a", "b"]]


def test_dag_detect_cycles_complex():
    dag = DAG()
    for node_id in ("a", "b", "c", "d"):
        dag.add_node(Node(id=node_id, kind="design_doc"))
    dag.add_edge(Edge(from_id="a", to_id="b", kind="depends_on"))
    dag.add_edge(Edge(from_id="b", to_id="c", kind="depends_on"))
    dag.add_edge(Edge(from_id="c", to_id="a", kind="depends_on"))
    dag.add_edge(Edge(from_id="c", to_id="d", kind="depends_on"))

    assert dag.detect_cycles() == [["a", "b", "c"]]


def test_dag_get_neighbors():
    dag = DAG()
    dag.add_edge(Edge(from_id="a", to_id="b", kind="depends_on"))
    dag.add_edge(Edge(from_id="a", to_id="c", kind="expects"))

    assert dag.get_neighbors("a") == ["b", "c"]


def test_dag_reverse_closure():
    dag = DAG()
    dag.add_edge(Edge(from_id="root", to_id="design", kind="depends_on"))
    dag.add_edge(Edge(from_id="design", to_id="impl", kind="expects"))
    dag.add_edge(Edge(from_id="sibling", to_id="impl", kind="references"))

    assert dag.reverse_closure("impl") == {"root", "design", "sibling"}


def test_register_dag_check_decorator(monkeypatch):
    monkeypatch.setattr(dag_checks, "_REGISTRY", {})

    @dag_checks.register_dag_check("node_completeness")
    class NodeCompletenessCheck:
        pass

    assert dag_checks.get_registry() == {"node_completeness": NodeCompletenessCheck}


def test_get_registry(monkeypatch):
    monkeypatch.setattr(dag_checks, "_REGISTRY", {})

    @dag_checks.register_dag_check("edge_validity")
    class EdgeValidityCheck:
        pass

    registry = dag_checks.get_registry()
    registry["other"] = object

    assert dag_checks.get_registry() == {"edge_validity": EdgeValidityCheck}


def test_run_all_checks_empty_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(dag_checks, "_REGISTRY", {})

    assert dag_checks.run_all_checks(DAG(), tmp_path, {"enabled_checks": []}) == []
