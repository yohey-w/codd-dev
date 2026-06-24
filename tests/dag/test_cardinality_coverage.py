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


# Normalization guard — a capitalized policy ("All") must behave like "all"; a
# case-sensitive compare would let it bypass the red path (a false-green).
def test_policy_all_is_case_insensitive():
    dag = _dag(
        _design_doc(
            data_dependencies=[ONE_TO_MANY_DEP],
            aggregation_policies=[
                _aggregation_policy("All", ["line_item:A_visible", "line_item:B_visible"]),
            ],
        ),
        _test_node(assertions=["line_item:A_visible"]),
    )
    result = _run(dag)
    assert result.severity == "red"
    assert _warnings_of_type(result, "cardinality_members_not_all_asserted")


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
# field_id="line_item" binds this assertion to the order->line_item relation
# (relation-identity match) so the unverifiable_all amber is emitted; with empty
# members there is no logical miss, so it can never be red.
def test_policy_all_empty_members_is_amber_not_red():
    dag = _dag(
        _design_doc(
            data_dependencies=[ONE_TO_MANY_DEP],
            aggregation_policies=[_aggregation_policy("all", [], field_id="line_item")],
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
    assert warnings[0]["field_id"] == "line_item"


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


# --- Relation-identity binding: assertions must bind to the detected relation ---
# An assertion is bound to a detected 1:N relation only when its field_id OR a
# member_signal entity-prefix norm-equals the relation's child (or parent). An
# assertion that corresponds to no detected relation is out of scope: it can
# neither produce red (anti-false-red) nor suppress the relation's own amber.


def _unrelated_policy(policy: str, member_signals: list[str]) -> dict:
    # field_id and member_signal entity-prefixes deliberately do NOT correspond to
    # the detected order->line_item relation (no "line_item"/"order" anywhere).
    return {
        "field_id": "unrelated_totals",
        "cardinality": "1:N",
        "cardinality_assertion": {
            "policy": policy,
            "member_signals": list(member_signals),
        },
    }


# anti-false-red regression: a 1:N relation (order->line_item) plus an UNRELATED
# field's policy=all with a missing member must NOT turn the run red. The
# unrelated assertion binds to no detected relation, so it is out of red scope.
def test_unrelated_policy_all_missing_does_not_make_relation_red():
    dag = _dag(
        _design_doc(
            data_dependencies=[ONE_TO_MANY_DEP],
            aggregation_policies=[
                _unrelated_policy("all", ["unrelated:X", "unrelated:Y"]),
            ],
        ),
        _test_node(assertions=["order:created"]),
    )
    result = _run(dag)
    assert result.passed is True
    assert result.block_deploy is False
    assert result.severity != "red"
    assert result.status != "fail"
    # No red violation may be raised for the unrelated, unbound assertion.
    assert _warnings_of_type(result, "cardinality_members_not_all_asserted") == []


# anti-false-green regression: an unrelated field declaring ANY policy must not
# suppress the detected relation's own cardinality_policy_unspecified amber. The
# order->line_item relation has no matching assertion, so it stays amber.
def test_unrelated_representative_does_not_suppress_relation_amber():
    dag = _dag(
        _design_doc(
            data_dependencies=[ONE_TO_MANY_DEP],
            aggregation_policies=[
                _unrelated_policy("representative", ["unrelated:X"]),
            ],
        ),
        _test_node(assertions=["unrelated:X"]),
    )
    result = _run(dag)
    assert result.passed is True
    assert result.block_deploy is False
    warnings = _warnings_of_type(result, "cardinality_policy_unspecified")
    assert len(warnings) >= 1
    assert any(
        w.get("parent") == "order" and w.get("child") == "line_item" for w in warnings
    )


# regression: a MATCHED field (member_signals carry the child prefix) with
# policy=all + a missing member is still red — relation-identity binding must not
# weaken the legitimate red path.
def test_matched_policy_all_missing_is_still_red():
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
    violations = _warnings_of_type(result, "cardinality_members_not_all_asserted")
    assert len(violations) == 1
    assert violations[0]["field_id"] == "line_items"
    assert violations[0]["missing_signals"] == ["line_item:B_visible"]


# Binding-evidence boundary: a PLURAL field_id ("line_items") with NO
# member_signals carries no exact-norm identity match to the singular child
# ("line_item"), so it stays UNBOUND. It must produce neither unverifiable_all
# nor red; the relation instead surfaces its own amber-by-omission. (Strict
# exact-norm matching — never substring/pluralization — keeps a loose bind from
# re-introducing a false-red.)
def test_plural_field_id_empty_members_stays_unbound():
    dag = _dag(
        _design_doc(
            data_dependencies=[ONE_TO_MANY_DEP],
            aggregation_policies=[_aggregation_policy("all", [], field_id="line_items")],
        ),
        _test_node(assertions=["order:created"]),
    )
    result = _run(dag)
    assert result.passed is True
    assert result.block_deploy is False
    assert result.severity != "red"
    # Unbound: no per-field unverifiable_all amber, no red violation.
    assert _warnings_of_type(result, "cardinality_unverifiable_all") == []
    assert _warnings_of_type(result, "cardinality_members_not_all_asserted") == []
    # The relation itself is still surfaced as amber-by-omission.
    unspecified = _warnings_of_type(result, "cardinality_policy_unspecified")
    assert any(
        w.get("parent") == "order" and w.get("child") == "line_item" for w in unspecified
    )
