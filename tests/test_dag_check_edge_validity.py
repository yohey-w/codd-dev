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

    assert set(result.__dict__) == {
        "check_name",
        "severity",
        "status",
        "orphan_edges",
        "dangling_refs",
        "checked_count",
        "passed",
    }


def test_empty_dag_pass_is_vacuous_checked_count_zero(tmp_path):
    # An empty DAG has no edges and no path-bearing nodes, so edge_validity passes
    # having verified nothing. checked_count==0 lets the materiality overlay flag
    # the vacuous pass instead of it reading as a verified clean run.
    from codd.dag.materiality import is_vacuous_pass

    result = EdgeValidityCheck().run(DAG(), tmp_path, {})

    assert result.passed is True
    assert result.checked_count == 0
    assert is_vacuous_pass(result) is True


def test_valid_dag_pass_is_not_vacuous(tmp_path):
    # A real verification inspects edges + path nodes, so checked_count is non-zero
    # and the pass is materially distinct from the empty (vacuous) case.
    from codd.dag.materiality import is_vacuous_pass

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("export const ok = true;\n", encoding="utf-8")
    dag = _dag_with_node("src/index.ts", "src/index.ts")
    dag.add_node(Node(id="src/feature.ts", kind="impl_file"))
    dag.add_edge(Edge(from_id="src/index.ts", to_id="src/feature.ts", kind="imports"))

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.checked_count >= 1
    assert is_vacuous_pass(result) is False


def test_node_without_path_no_dangling(tmp_path):
    dag = _dag_with_node("virtual", None)

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.dangling_refs == []


def test_in_root_absolute_path_pass(tmp_path):
    """An absolute node.path inside project_root that exists stays valid (anti-false-red)."""
    target = tmp_path / "src" / "index.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("export const ok = true;\n", encoding="utf-8")
    dag = _dag_with_node(str(target), str(target))

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is True
    assert result.dangling_refs == []


def test_out_of_root_absolute_path_red(tmp_path):
    """An absolute node.path outside project_root is dangling/spoof even if it exists on disk."""
    outside_root = tmp_path.parent / (tmp_path.name + "_outside")
    outside_root.mkdir(parents=True, exist_ok=True)
    escapee = outside_root / "evil.py"
    escapee.write_text("x = 1\n", encoding="utf-8")
    assert escapee.exists()
    dag = _dag_with_node(str(escapee), str(escapee))

    result = EdgeValidityCheck().run(dag, tmp_path, {})

    assert result.passed is False
    assert str(escapee) in result.dangling_refs


def test_out_of_root_relative_traversal_path_red(tmp_path):
    """A node.path that traverses out of project_root via ``..`` is dangling even if it exists."""
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    escapee = outside / "evil.py"
    escapee.write_text("x = 1\n", encoding="utf-8")
    node_path = "../outside/evil.py"
    assert (project_root / node_path).exists()
    dag = _dag_with_node(node_path, node_path)

    result = EdgeValidityCheck().run(dag, project_root, {})

    assert result.passed is False
    assert node_path in result.dangling_refs
