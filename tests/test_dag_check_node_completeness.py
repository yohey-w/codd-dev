from __future__ import annotations

import importlib
from pathlib import Path

from codd.dag import DAG, Edge, Node
from codd.dag import checks as dag_checks
from codd.dag.checks.node_completeness import NodeCompletenessCheck, NodeCompletenessResult


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _dag_with_design() -> DAG:
    dag = DAG()
    dag.add_node(Node(id="docs/design/ux_design.md", kind="design_doc", path="docs/design/ux_design.md"))
    return dag


def _run(dag: DAG, project_root: Path) -> NodeCompletenessResult:
    return NodeCompletenessCheck().run(dag, project_root, {})


def test_node_completeness_registered(monkeypatch):
    monkeypatch.setattr(dag_checks, "_REGISTRY", {})

    module = importlib.reload(importlib.import_module("codd.dag.checks.node_completeness"))

    assert dag_checks.get_registry()["node_completeness"] is module.NodeCompletenessCheck


def test_no_expects_edges_pass(tmp_path):
    dag = _dag_with_design()

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.missing_impl_files == []


def test_expects_existing_node_pass(tmp_path):
    _write(tmp_path / "app" / "page.tsx")
    dag = _dag_with_design()
    dag.add_node(Node(id="app/page.tsx", kind="impl_file", path="app/page.tsx"))
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="app/page.tsx", kind="expects"))

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.missing_impl_files == []


def test_expects_missing_node_fail(tmp_path):
    dag = _dag_with_design()
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="app/missing/page.tsx", kind="expects"))

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == ["app/missing/page.tsx"]


def test_expects_missing_file_on_disk_fail(tmp_path):
    dag = _dag_with_design()
    dag.add_node(Node(id="app/page.tsx", kind="impl_file", path="app/page.tsx"))
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="app/page.tsx", kind="expects"))

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == ["app/page.tsx"]


def test_multiple_missing_collected(tmp_path):
    dag = _dag_with_design()
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="app/admin/page.tsx", kind="expects"))
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="app/student/page.tsx", kind="expects"))

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == ["app/admin/page.tsx", "app/student/page.tsx"]


def test_severity_is_red():
    assert NodeCompletenessResult().severity == "red"


def test_osato_lms_role_home_detection(tmp_path):
    dag = _dag_with_design()
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="app/(roles)/admin/page.tsx", kind="expects"))

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == ["app/(roles)/admin/page.tsx"]


def test_result_dataclass_fields():
    result = NodeCompletenessResult(missing_impl_files=["app/page.tsx"], passed=False)

    assert result.check_name == "node_completeness"
    assert result.severity == "red"
    assert result.missing_impl_files == ["app/page.tsx"]
    assert result.passed is False


def test_passed_flag_true_on_no_missing(tmp_path):
    dag = _dag_with_design()

    assert _run(dag, tmp_path).passed is True


def test_passed_flag_false_on_missing(tmp_path):
    dag = _dag_with_design()
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="app/page.tsx", kind="expects"))

    assert _run(dag, tmp_path).passed is False


def test_empty_dag_pass(tmp_path):
    result = _run(DAG(), tmp_path)

    assert result.passed is True
    assert result.missing_impl_files == []
