from __future__ import annotations

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


def test_t05_no_one_to_many_relations_is_noop_pass():
    result = _run(_dag(_design("docs/design/ux_design.md", "plain UI design")))

    assert result.one_to_many_relations_total == 0
    assert result.relations_missing_master_detail == []
    assert result.passed is True


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
