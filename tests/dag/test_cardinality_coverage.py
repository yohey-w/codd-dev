"""Tests for the cardinality_coverage DAG check (1:N member coverage).

A 1:N relation only proves a relation exists — never the member universe. So:

* red is reachable ONLY when the design doc explicitly declares
  ``cardinality_assertion.policy: all`` with a non-empty ``member_signals`` list
  and at least one of those signals is provably not asserted (a logical miss).
* every other shape is amber at most: an unspecified policy, an empty-member
  ``all`` (unverifiable), ``representative`` (passes with a limitation summary),
  or ``at_least_one`` satisfied by a single asserted member.
* no 1:N relation ⇒ skip (checked_count=0). The member universe is never
  inferred from the relation detector.
"""

from __future__ import annotations

from codd.dag import DAG, Node
from codd.dag.checks import get_registry
from codd.dag.checks.cardinality_coverage import CardinalityCoverageCheck


def _dag(*nodes: Node) -> DAG:
    dag = DAG()
    for node in nodes:
        dag.add_node(node)
    return dag


def _design_doc(node_id: str = "docs/design/orders.md", **attributes) -> Node:
    return Node(id=node_id, kind="design_doc", attributes=attributes)


def _test_node(node_id: str = "tests/e2e/orders.test.ts", **attributes) -> Node:
    return Node(id=node_id, kind="test_file", attributes=attributes)


def _run(dag: DAG, project_root=None):
    return CardinalityCoverageCheck(dag, project_root).run()


def _warnings_of_type(result, warning_type: str) -> list[dict]:
    return [w for w in result.warnings if w.get("type") == warning_type]


# A 1:N relation that the schema-light detector picks up from data_dependencies,
# co-located on the design doc so the aggregation_policies live on the same node.
ONE_TO_MANY_DEP = {
    "parent": "order",
    "child": "line_item",
    "cardinality": "1:N",
}


def _aggregation_policy(policy: str, member_signals: list[str], field_id: str = "line_items") -> dict:
    return {
        "field_id": field_id,
        "cardinality": "1:N",
        "cardinality_assertion": {
            "policy": policy,
            "member_signals": list(member_signals),
        },
    }


def test_cardinality_coverage_registered():
    assert get_registry()["cardinality_coverage"] is CardinalityCoverageCheck


# Guard: no 1:N relation anywhere ⇒ dormant skip, nothing verified.
def test_no_one_to_many_relation_skips():
    dag = _dag(_design_doc(), _test_node(assertions=["anything"]))
    result = _run(dag)
    assert result.skipped is True
    assert result.status == "skip"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 0
    assert result.warnings == []


# Fixture 1 — policy=all, 2 member_signals, both asserted ⇒ pass.
def test_policy_all_all_members_asserted_passes():
    dag = _dag(
        _design_doc(
            data_dependencies=[ONE_TO_MANY_DEP],
            aggregation_policies=[
                _aggregation_policy("all", ["line_item:A_visible", "line_item:B_visible"]),
            ],
        ),
        _test_node(assertions=["line_item:A_visible", "line_item:B_visible"]),
    )
    result = _run(dag)
    assert result.status == "pass"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.skipped is False
    assert result.checked_count >= 1
    assert result.warnings == []
    summary = next(s for s in result.summaries if s["field_id"] == "line_items")
    assert summary["policy"] == "all"
    assert summary["status"] == "complete_all"
    assert summary["asserted"] == 2


# Fixture 2 — policy=all, 2 members but only 1 asserted ⇒ red (logical miss).
def test_policy_all_one_member_missing_is_red():
    dag = _dag(
        _design_doc(
            data_dependencies=[ONE_TO_MANY_DEP],
            aggregation_policies=[
                _aggregation_policy("all", ["line_item:A_visible", "line_item:B_visible"]),
            ],
        ),
        _test_node(assertions=["line_item:A_visible"]),
    )
    result = _run(dag)
    assert result.status == "fail"
    assert result.severity == "red"
    assert result.passed is False
    assert result.block_deploy is True
    assert result.checked_count >= 1
    violations = _warnings_of_type(result, "cardinality_members_not_all_asserted")
    assert len(violations) == 1
    violation = violations[0]
    assert violation["field_id"] == "line_items"
    assert violation["missing_signals"] == ["line_item:B_visible"]
    assert violation["severity"] == "red"
    assert violation["block_deploy"] is True


# Fixture 3 — policy=representative, 1 member asserted ⇒ pass + limitation summary.
def test_policy_representative_passes_with_summary():
    dag = _dag(
        _design_doc(
            data_dependencies=[ONE_TO_MANY_DEP],
            aggregation_policies=[
                _aggregation_policy("representative", ["line_item:A_visible"]),
            ],
        ),
        _test_node(assertions=["line_item:A_visible"]),
    )
    result = _run(dag)
    assert result.status == "pass"
    assert result.passed is True
    assert result.block_deploy is False
    summary = next(s for s in result.summaries if s["field_id"] == "line_items")
    assert summary["policy"] == "representative"
    assert summary["status"] == "representative"
    assert "limitation" in summary
    assert summary["limitation"]


# Fixture 4 — 1:N relation detected, verification exists, no policy ⇒ amber.
def test_one_to_many_no_policy_is_amber():
    dag = _dag(
        _design_doc(data_dependencies=[ONE_TO_MANY_DEP]),
        _test_node(assertions=["order:created"]),
    )
    result = _run(dag)
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count >= 1
    warnings = _warnings_of_type(result, "cardinality_policy_unspecified")
    assert len(warnings) >= 1
    warning = warnings[0]
    assert warning["parent"] == "order"
    assert warning["child"] == "line_item"
    assert warning["severity"] == "amber"
    assert warning["block_deploy"] is False


# Guard: at_least_one is satisfied by a SINGLE asserted member (never the full
# unknown universe) ⇒ pass, no red.
def test_policy_at_least_one_single_member_passes():
    dag = _dag(
        _design_doc(
            data_dependencies=[ONE_TO_MANY_DEP],
            aggregation_policies=[
                _aggregation_policy(
                    "at_least_one",
                    ["line_item:A_visible", "line_item:B_visible"],
                ),
            ],
        ),
        _test_node(assertions=["line_item:A_visible"]),
    )
    result = _run(dag)
    assert result.status == "pass"
    assert result.passed is True
    assert result.block_deploy is False
    summary = next(s for s in result.summaries if s["field_id"] == "line_items")
    assert summary["status"] == "satisfied_at_least_one"


# Guard: policy=all but member_signals empty ⇒ amber (unverifiable all), NOT red.
def test_policy_all_empty_members_is_amber_not_red():
    dag = _dag(
        _design_doc(
            data_dependencies=[ONE_TO_MANY_DEP],
            aggregation_policies=[_aggregation_policy("all", [])],
        ),
        _test_node(assertions=["something"]),
    )
    result = _run(dag)
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    warnings = _warnings_of_type(result, "cardinality_unverifiable_all")
    assert len(warnings) == 1
    assert warnings[0]["field_id"] == "line_items"


# Guard: heuristic relation hit with NO verification present ⇒ no red, no amount
# of relation detection manufactures a member universe; deploy is never blocked.
def test_relation_without_verification_does_not_block():
    dag = _dag(_design_doc(data_dependencies=[ONE_TO_MANY_DEP]))
    result = _run(dag)
    assert result.passed is True
    assert result.block_deploy is False
    assert result.status == "pass"
    # No verification => the unspecified-policy amber is suppressed entirely.
    assert _warnings_of_type(result, "cardinality_policy_unspecified") == []
