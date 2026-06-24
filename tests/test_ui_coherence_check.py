from __future__ import annotations

import os
from pathlib import Path

from codd.dag import DAG, Node
from codd.dag.checks import get_registry
from codd.dag.checks.ui_coherence import UiCoherenceCheck


def _dag(*nodes: Node) -> DAG:
    dag = DAG()
    for node in nodes:
        dag.add_node(node)
    return dag


def _lexicon(term: str, description: str) -> Node:
    return Node(
        id=f"lexicon:{term}",
        kind="lexicon",
        attributes={"term": term, "description": description},
    )


def _design(node_id: str, content: str, attributes: dict | None = None) -> Node:
    merged = {"content": content}
    if attributes:
        merged.update(attributes)
    return Node(id=node_id, kind="design_doc", path=node_id, attributes=merged)


def _run(dag: DAG, config: dict | None = None):
    return UiCoherenceCheck(dag=dag, project_root=None, settings={}).run(codd_config=config or {})


def test_ui_coherence_registered():
    assert get_registry()["ui_coherence_for_one_to_many"] is UiCoherenceCheck


def test_t01_lexicon_relation_with_master_detail_ui_passes():
    dag = _dag(
        _lexicon("delivery_target", "delivery_target is many-to-one with course"),
        _design(
            "docs/design/ux_design.md",
            "course delivery_target master-detail page at /courses/[id]/delivery-targets",
        ),
    )

    result = _run(dag)

    assert result.one_to_many_relations_total == 1
    assert result.relations_with_master_detail_ui == 1
    assert result.relations_missing_master_detail == []


def test_t02_lexicon_relation_without_master_detail_warns_but_allows_deploy():
    dag = _dag(
        _lexicon("delivery_target", "delivery_target is many-to-one with course"),
        _design("docs/design/ux_design.md", "course delivery_target are edited on one generic admin screen"),
    )

    result = _run(dag)

    assert result.severity == "amber"
    assert result.block_deploy is False
    assert result.passed is True
    assert result.relations_missing_master_detail[0]["parent"] == "course"
    assert result.relations_missing_master_detail[0]["child"] == "delivery_target"
    assert result.warnings


def test_t03_single_form_operation_flow_suppresses_warning():
    dag = _dag(
        _lexicon("delivery_target", "delivery_target is many-to-one with course"),
        _design(
            "docs/design/requirements.md",
            "requirements",
            {
                "operation_flow": {
                    "operations": [
                        {
                            "id": "configure_delivery",
                            "target": "delivery_target",
                            "parent": "course",
                            "ui_pattern": "single_form",
                        }
                    ]
                }
            },
        ),
    )

    result = _run(dag)

    assert result.relations_missing_master_detail == []
    assert result.suppressed_relations == 1


def test_t04_ignore_relations_config_suppresses_warning():
    dag = _dag(_lexicon("delivery_target", "delivery_target is many-to-one with course"))

    result = _run(
        dag,
        {"ui_coherence": {"ignore_relations": [{"parent": "course", "child": "delivery_target"}]}},
    )

    assert result.relations_missing_master_detail == []
    assert result.ignored_relations == 1


def test_t05_no_one_to_many_relations_skips_not_vacuous_pass():
    # No one-to-many relation = no master-detail obligation to verify. The check
    # must SKIP (checked nothing on purpose), not emit a clean PASS over 0 relations
    # that a verify summary cannot tell apart from a real verification (false-green).
    result = _run(_dag(_design("docs/design/ux_design.md", "plain UI design")))

    assert result.one_to_many_relations_total == 0
    assert result.relations_missing_master_detail == []
    assert result.passed is True
    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0


def test_t06_db_table_relation_with_drilldown_ui_passes():
    dag = _dag(
        Node(
            id="db_table:course",
            kind="db_table",
            attributes={"relations": [{"target": "delivery_target", "cardinality": "1:N"}]},
        ),
        _design(
            "docs/design/ux_design.md",
            "course delivery_target drilldown at /courses/[id]/delivery-targets",
        ),
    )

    result = _run(dag)

    assert result.one_to_many_relations_total == 1
    assert result.relations_with_master_detail_ui == 1
    assert result.relations_missing_master_detail == []


def test_t07_japanese_drilldown_literal_counts_as_master_detail_ui():
    dag = _dag(
        _lexicon("delivery_target", "delivery_target is many-to-one with course"),
        _design("docs/design/ux_design.md", "course から delivery_target へドリルダウンする詳細画面"),
    )

    result = _run(dag)

    assert result.relations_with_master_detail_ui == 1
    assert result.relations_missing_master_detail == []


def _run_rooted(dag: DAG, root: Path):
    return UiCoherenceCheck(dag=dag, project_root=root, settings={}).run(codd_config={})


def test_t08_design_doc_node_path_out_of_root_not_credited(tmp_path):
    """A design_doc node.path OUT OF ROOT must not PASS-credit master-detail UI.

    The one-to-many relation needs master-detail evidence. A node whose only
    in-attribute text is absent but whose ``path`` points to an out-of-root file
    containing the master-detail wording would, unjailed, have its file read and
    credit the relation — a path-escape false-green. The jail refuses to read it,
    so the relation stays missing.
    """
    outside = tmp_path.parent / "outside_ux.md"
    outside.write_text(
        "course delivery_target master-detail page at /courses/[id]/delivery-targets\n",
        encoding="utf-8",
    )

    dag = _dag(
        _lexicon("delivery_target", "delivery_target is many-to-one with course"),
        # node.path is absolute + out of root; no in-attribute content provided.
        Node(id="ux_design", kind="design_doc", path=str(outside), attributes={}),
    )

    result = _run_rooted(dag, tmp_path)

    assert result.one_to_many_relations_total == 1
    assert result.relations_with_master_detail_ui == 0
    assert result.relations_missing_master_detail
    assert result.relations_missing_master_detail[0]["parent"] == "course"
    assert result.relations_missing_master_detail[0]["child"] == "delivery_target"


def test_t09_design_doc_node_path_symlink_escape_not_credited(tmp_path):
    """An in-root design_doc symlink whose target escapes must not PASS-credit UI.

    Same path-escape class via an in-root symlink resolving outside the tree.
    """
    outside = tmp_path.parent / "outside_ux2.md"
    outside.write_text(
        "course delivery_target drilldown at /courses/[id]/delivery-targets\n",
        encoding="utf-8",
    )
    link = tmp_path / "docs" / "design" / "ux_design.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link)

    dag = _dag(
        _lexicon("delivery_target", "delivery_target is many-to-one with course"),
        Node(id="docs/design/ux_design.md", kind="design_doc", path="docs/design/ux_design.md", attributes={}),
    )

    result = _run_rooted(dag, tmp_path)

    assert result.one_to_many_relations_total == 1
    assert result.relations_with_master_detail_ui == 0
    assert result.relations_missing_master_detail


def test_t10_in_root_design_doc_file_still_credited(tmp_path):
    """Anti-false-red: an in-root design_doc file is read and still credits UI.

    Confirms the jail does not break the legitimate on-disk read path.
    """
    doc = tmp_path / "docs" / "design" / "ux_design.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "course delivery_target master-detail page at /courses/[id]/delivery-targets\n",
        encoding="utf-8",
    )

    dag = _dag(
        _lexicon("delivery_target", "delivery_target is many-to-one with course"),
        Node(id="docs/design/ux_design.md", kind="design_doc", path="docs/design/ux_design.md", attributes={}),
    )

    result = _run_rooted(dag, tmp_path)

    assert result.one_to_many_relations_total == 1
    assert result.relations_with_master_detail_ui == 1
    assert result.relations_missing_master_detail == []
