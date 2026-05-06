from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from codd.dag import DAG, Edge, Node
from codd.repair.primary_picker import FirstViolationPicker, PrimaryPicker


@dataclass
class Violation:
    affected_nodes: list
    severity: str = "info"
    timestamp: object = "2026-05-06T00:00:00Z"


def _dag(edges: list[tuple[str, str]]) -> DAG:
    dag = DAG()
    node_ids = sorted({node_id for edge in edges for node_id in edge})
    for node_id in node_ids:
        dag.add_node(Node(node_id, "artifact"))
    for left, right in edges:
        dag.add_edge(Edge(left, right, "relates"))
    return dag


def test_empty_violations_return_none():
    assert PrimaryPicker().pick([], _dag([])) is None


def test_single_violation_preserves_existing_first_choice():
    violation = Violation(["leaf"])

    assert PrimaryPicker().pick([violation], _dag([("root", "leaf")])) is violation


def test_upstream_violation_wins_over_downstream_severity():
    root = Violation(["root"], severity="info")
    leaf = Violation(["leaf"], severity="critical")

    assert PrimaryPicker().pick([leaf, root], _dag([("root", "mid"), ("mid", "leaf")])) is root


def test_same_level_uses_severity_order():
    low = Violation(["left"], severity="info")
    mid = Violation(["right"], severity="medium")
    high = Violation(["third"], severity="high")
    critical = Violation(["fourth"], severity="critical")

    assert PrimaryPicker().pick([low, mid, high, critical], _dag([])) is critical


def test_severity_matching_is_case_insensitive():
    high = Violation(["left"], severity="HIGH")
    medium = Violation(["right"], severity="medium")

    assert PrimaryPicker().pick([medium, high], _dag([])) is high


def test_same_level_and_severity_uses_oldest_timestamp():
    newer = Violation(["left"], severity="high", timestamp="2026-05-06T10:00:00Z")
    older = Violation(["right"], severity="high", timestamp="2026-05-06T09:00:00Z")

    assert PrimaryPicker().pick([newer, older], _dag([])) is older


def test_datetime_timestamp_is_supported():
    newer = Violation(["left"], severity="high", timestamp=datetime(2026, 5, 6, 10, tzinfo=timezone.utc))
    older = Violation(["right"], severity="high", timestamp=datetime(2026, 5, 6, 9, tzinfo=timezone.utc))

    assert PrimaryPicker().pick([newer, older], _dag([])) is older


def test_multiple_affected_nodes_use_most_upstream_member():
    spanning = Violation(["leaf", "root"], severity="info")
    mid_only = Violation(["mid"], severity="critical")

    assert PrimaryPicker().pick([mid_only, spanning], _dag([("root", "mid"), ("mid", "leaf")])) is spanning


def test_mapping_violation_and_mapping_node_entries_are_supported():
    raw = {"affected_nodes": [{"id": "root"}], "severity": "info", "timestamp": "2026-05-06T00:00:00Z"}
    other = {"affected_nodes": ["leaf"], "severity": "critical", "timestamp": "2026-05-06T00:00:00Z"}

    assert PrimaryPicker().pick([other, raw], _dag([("root", "leaf")])) is raw


def test_mapping_dag_snapshot_is_supported():
    dag = {
        "nodes": [{"id": "root"}, {"id": "leaf"}],
        "edges": [{"from_id": "root", "to_id": "leaf", "kind": "relates"}],
    }
    root = Violation(["root"])
    leaf = Violation(["leaf"], severity="critical")

    assert PrimaryPicker().pick([leaf, root], dag) is root


def test_cycle_uses_safe_deterministic_level():
    first = Violation(["a"], severity="medium", timestamp="2026-05-06T09:00:00Z")
    second = Violation(["b"], severity="high", timestamp="2026-05-06T10:00:00Z")

    assert PrimaryPicker().pick([first, second], _dag([("a", "b"), ("b", "a")])) is second


def test_first_violation_picker_fallback():
    first = Violation(["root"])
    second = Violation(["leaf"], severity="critical")

    assert FirstViolationPicker().pick([first, second], _dag([("root", "leaf")])) is first
    assert FirstViolationPicker().pick([], _dag([])) is None


def test_generality_gate_has_no_domain_or_kind_literals():
    pattern = "lms|osato|web app|mobile app|cli|backend|embedded|requirement|design|implementation|test|node_completeness|deployment_completeness|vitest"

    result = subprocess.run(
        ["grep", "-rEi", pattern, "codd/repair/primary_picker.py"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
