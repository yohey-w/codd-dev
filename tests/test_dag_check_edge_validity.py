import importlib

from codd.dag import DAG, Edge, Node
from codd.dag import checks as dag_checks
from codd.dag.checks import edge_validity
from codd.dag.checks.edge_validity import EdgeValidityCheck


def _dag_with_node(node_id: str, path: str | None = None) -> DAG:
    dag = DAG()
    dag.add_node(Node(id=node_id, kind="impl_file", path=path))
    return dag


def test_edge_validity_registered(monkeypatch, tmp_path):
    monkeypatch.setattr(dag_checks, "_REGISTRY", {})

    module = importlib.reload(edge_validity)

    assert dag_checks.get_registry()["edge_validity"] is module.EdgeValidityCheck
    assert dag_checks.run_all_checks(DAG(), tmp_path, {})[0].check_name == "edge_validity"


def test_valid_dag_pass(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("export const ok = true;\n", encoding="utf-8")
    dag = _dag_with_node("src/index.ts", "src/index.ts")
    dag.add_node(Node(id="src/feature.ts", kind="impl_file"))
    dag.add_edge(Edge(from_id="src/index.ts", to_id="src/feature.ts", kind="imports"))

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.orphan_edges == []
    assert result.dangling_refs == []


def test_orphan_edge_from_missing_fail(tmp_path):
    dag = _dag_with_node("target")
    dag.add_edge(Edge(from_id="missing", to_id="target", kind="depends_on"))

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is False
    assert result.orphan_edges == [{"from_id": "missing", "to_id": "target", "kind": "depends_on"}]


def test_orphan_edge_to_missing_fail(tmp_path):
    dag = _dag_with_node("source")
    dag.add_edge(Edge(from_id="source", to_id="missing", kind="expects"))

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is False
    assert result.orphan_edges == [{"from_id": "source", "to_id": "missing", "kind": "expects"}]


def test_dangling_ref_fail(tmp_path):
    dag = _dag_with_node("src/missing.ts", "src/missing.ts")

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is False
    assert result.dangling_refs == ["src/missing.ts"]


def test_multiple_orphans_collected(tmp_path):
    dag = _dag_with_node("source")
    dag.add_edge(Edge(from_id="missing-a", to_id="source", kind="depends_on"))
    dag.add_edge(Edge(from_id="source", to_id="missing-b", kind="expects"))

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is False
    assert result.orphan_edges == [
        {"from_id": "missing-a", "to_id": "source", "kind": "depends_on"},
        {"from_id": "source", "to_id": "missing-b", "kind": "expects"},
    ]


def test_empty_dag_pass(tmp_path):
    result = EdgeValidityCheck().run(DAG(), tmp_path, {})

    assert result.passed is True
    assert result.orphan_edges == []
    assert result.dangling_refs == []


def test_severity_is_red(tmp_path):
    result = EdgeValidityCheck().run(DAG(), tmp_path, {})

    assert result.severity == "red"


def test_passed_flag_true_on_valid(tmp_path):
    dag = _dag_with_node("design")

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is True


def test_passed_flag_false_on_orphan(tmp_path):
    dag = DAG()
    dag.add_edge(Edge(from_id="missing-a", to_id="missing-b", kind="imports"))

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is False


def test_result_fields_present(tmp_path):
    result = EdgeValidityCheck().run(DAG(), tmp_path, {})

    assert set(result.__dict__) == {"check_name", "severity", "orphan_edges", "dangling_refs", "passed"}


def test_node_without_path_no_dangling(tmp_path):
    dag = _dag_with_node("virtual", None)

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.dangling_refs == []
