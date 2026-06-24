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


def test_no_expects_edges_skips(tmp_path):
    """No ``expects`` edge means nothing was verified — a SKIP, not a clean PASS.

    This is a severity=red gate. Previously it returned a green ``PASS [red]``
    having checked zero edges (a vacuous false-green). With no input it must now
    SKIP (verified nothing on purpose), and ``checked_count`` is 0 so the
    materiality overlay does not even need to flag it.
    """
    from codd.dag.materiality import is_vacuous_pass

    dag = _dag_with_design()

    result = _run(dag, tmp_path)

    assert result.passed is True  # no missing files, deploy still allowed
    assert result.missing_impl_files == []
    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0
    # A skip verified nothing on purpose → not flagged vacuous by the overlay.
    assert is_vacuous_pass(result) is False


def test_expects_existing_node_pass_counts_checked(tmp_path):
    """A real verification (>=1 expects edge satisfied) is a genuine PASS.

    Behaviour unchanged from before the vacuous-pass fix EXCEPT it now reports
    ``status='pass'`` and ``checked_count`` so the overlay can tell it apart
    from a vacuous one.
    """
    from codd.dag.materiality import is_vacuous_pass

    _write(tmp_path / "app" / "page.tsx")
    dag = _dag_with_design()
    dag.add_node(Node(id="app/page.tsx", kind="impl_file", path="app/page.tsx"))
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="app/page.tsx", kind="expects"))

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.missing_impl_files == []
    assert result.status == "pass"
    assert result.skipped is False
    assert result.checked_count == 1
    assert is_vacuous_pass(result) is False


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


def test_example_lms_role_home_detection(tmp_path):
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


def test_empty_dag_skips(tmp_path):
    """Empty DAG = zero expects edges = nothing verified → SKIP, not vacuous PASS."""
    result = _run(DAG(), tmp_path)

    assert result.passed is True
    assert result.missing_impl_files == []
    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0


def test_expects_missing_node_fail_reports_status_and_count(tmp_path):
    """A real failure (expects edge unsatisfied) is FAIL, with checked_count>=1.

    Locks in that a check that actually ran an edge is never a skip even when it
    fails — only no-input skips.
    """
    dag = _dag_with_design()
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="app/missing/page.tsx", kind="expects"))

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == ["app/missing/page.tsx"]
    assert result.status == "fail"
    assert result.skipped is False
    assert result.checked_count == 1


def test_expects_impl_node_path_out_of_root_is_missing(tmp_path):
    """An impl_file node.path pointing OUT OF ROOT must not satisfy ``expects``.

    A user-controllable absolute node.path (e.g. ``/etc/hosts``) happens to exist
    on the real filesystem, but no out-of-root file is the project's impl artifact.
    Counting it as "exists" is a path-escape false-green: a spoofed expected file
    silently passes. After the path_safety jail it is treated as missing (red).
    """
    dag = _dag_with_design()
    dag.add_node(Node(id="impl:hosts", kind="impl_file", path="/etc/hosts"))
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="impl:hosts", kind="expects"))

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == ["impl:hosts"]


def test_expects_impl_node_path_symlink_escape_is_missing(tmp_path):
    """An in-root symlink whose target escapes the tree must not satisfy ``expects``.

    The symlink lives inside the project, but resolves to an out-of-root file, so
    it cannot be the project's own impl artifact. The jail rejects it (missing/red),
    closing the symlink-escape false-green.
    """
    import os

    outside = tmp_path.parent / "outside_impl.tsx"
    outside.write_text("ok\n", encoding="utf-8")
    link = tmp_path / "app" / "page.tsx"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link)

    dag = _dag_with_design()
    dag.add_node(Node(id="app/page.tsx", kind="impl_file", path="app/page.tsx"))
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="app/page.tsx", kind="expects"))

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == ["app/page.tsx"]


def test_expects_common_node_path_out_of_root_is_missing(tmp_path):
    """A ``common`` node.path pointing out of root must not satisfy ``expects``.

    Same path-escape class as the impl_file branch but on the ``common`` node path
    (node_completeness.py:60). An absolute out-of-root path is treated as missing.
    """
    dag = _dag_with_design()
    dag.add_node(Node(id="common:hosts", kind="common", path="/etc/hosts"))
    dag.add_edge(Edge(from_id="docs/design/ux_design.md", to_id="common:hosts", kind="expects"))

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == ["common:hosts"]
