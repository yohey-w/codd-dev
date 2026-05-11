"""node_completeness handling for kind=common nodes (cmd_470 v2.17.0).

Background: kind="common" was introduced in v2.15.0 (cmd_467) so that shared
infrastructure could live on the DAG without forcing every consumer to declare
a producing design doc. transitive_closure was updated to skip common nodes,
but node_completeness still rejected them because the check only recognised
"impl_file" and "expected" — so any `expects` edge pointing at a common node
was incorrectly reported as a missing implementation file.

These tests pin the v2.17.0 behaviour:
- common node referenced by an `expects` edge is treated like impl_file for
  path existence (must exist on disk if a path is declared).
- common node without a declared path is allowed (no file-system check).
- legacy impl_file / expected / unknown-node behaviour is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from codd.dag import DAG, Edge, Node
from codd.dag.checks.node_completeness import NodeCompletenessCheck


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _dag_with_design() -> DAG:
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/spec.md",
            kind="design_doc",
            path="docs/design/spec.md",
        )
    )
    return dag


def _run(dag: DAG, project_root: Path):
    return NodeCompletenessCheck().run(dag, project_root, {})


def test_common_node_with_existing_path_passes(tmp_path):
    _write(tmp_path / "src" / "lib" / "shared.ts")
    dag = _dag_with_design()
    dag.add_node(
        Node(id="src/lib/shared.ts", kind="common", path="src/lib/shared.ts")
    )
    dag.add_edge(
        Edge(from_id="docs/design/spec.md", to_id="src/lib/shared.ts", kind="expects")
    )

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.missing_impl_files == []


def test_common_node_with_missing_path_fails(tmp_path):
    dag = _dag_with_design()
    dag.add_node(
        Node(id="src/lib/shared.ts", kind="common", path="src/lib/shared.ts")
    )
    dag.add_edge(
        Edge(from_id="docs/design/spec.md", to_id="src/lib/shared.ts", kind="expects")
    )

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == ["src/lib/shared.ts"]


def test_common_node_without_path_passes(tmp_path):
    """common nodes may be declared without a path (e.g. virtual shared concepts)."""
    dag = _dag_with_design()
    dag.add_node(Node(id="shared:auth_context", kind="common", path=None))
    dag.add_edge(
        Edge(
            from_id="docs/design/spec.md",
            to_id="shared:auth_context",
            kind="expects",
        )
    )

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.missing_impl_files == []


def test_mixed_common_and_impl_file_reports_only_real_misses(tmp_path):
    _write(tmp_path / "src" / "lib" / "good_common.ts")
    _write(tmp_path / "src" / "feature" / "good_impl.ts")
    dag = _dag_with_design()
    dag.add_node(
        Node(
            id="src/lib/good_common.ts",
            kind="common",
            path="src/lib/good_common.ts",
        )
    )
    dag.add_node(
        Node(
            id="src/lib/bad_common.ts",
            kind="common",
            path="src/lib/bad_common.ts",
        )
    )
    dag.add_node(
        Node(
            id="src/feature/good_impl.ts",
            kind="impl_file",
            path="src/feature/good_impl.ts",
        )
    )
    dag.add_node(
        Node(
            id="src/feature/bad_impl.ts",
            kind="impl_file",
            path="src/feature/bad_impl.ts",
        )
    )
    for to_id in (
        "src/lib/good_common.ts",
        "src/lib/bad_common.ts",
        "src/feature/good_impl.ts",
        "src/feature/bad_impl.ts",
    ):
        dag.add_edge(
            Edge(from_id="docs/design/spec.md", to_id=to_id, kind="expects")
        )

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == [
        "src/lib/bad_common.ts",
        "src/feature/bad_impl.ts",
    ]


def test_expected_kind_still_passes_unchanged(tmp_path):
    """Legacy: kind=expected continues to be allowed without disk check."""
    dag = _dag_with_design()
    dag.add_node(
        Node(id="planned:future_module", kind="expected", path=None)
    )
    dag.add_edge(
        Edge(
            from_id="docs/design/spec.md",
            to_id="planned:future_module",
            kind="expects",
        )
    )

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.missing_impl_files == []


def test_unknown_kind_still_reports_missing(tmp_path):
    """Legacy: a node with an unrecognised kind continues to be flagged."""
    dag = _dag_with_design()
    dag.add_node(Node(id="some/unknown.txt", kind="lexicon", path=None))
    dag.add_edge(
        Edge(from_id="docs/design/spec.md", to_id="some/unknown.txt", kind="expects")
    )

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.missing_impl_files == ["some/unknown.txt"]
