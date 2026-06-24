"""Tests for the semantic_contract_conflict DAG check (structural scalar conflict).

This check does NOT do general (NLP) meaning-conflict detection. It surfaces only
the narrow *structural* case: the **same identity** declaring the **same scalar
property** with **two different values** — e.g. one ``aggregation_policies`` entry
saying ``policy: all`` and another, for the same ``field_id``, saying
``policy: representative``. That is a contradiction the project cannot have meant,
and it is decidable without judgement.

Guards exercised here:

* **amber only — never red.** A scalar contradiction is an authoring ambiguity to
  surface, not a deploy blocker.
* **scalar values only.** ``list`` / ``dict`` / free-text values are out of scope
  (no "速い vs 厳密"-style judgement).
* **declared values only.** Conflicts are never manufactured from default
  backfill — only values the project actually wrote are compared.
* **same section + same identity + same key only.** Different fields, different
  sections, or different keys never conflict.
* **dormant by default.** No target section ⇒ ``skip`` (exit code unaffected).
"""

from __future__ import annotations

from codd.dag import DAG, Node
from codd.dag.checks import get_registry
from codd.dag.checks.semantic_contract_conflict import SemanticContractConflictCheck


def _dag(*nodes: Node) -> DAG:
    dag = DAG()
    for node in nodes:
        dag.add_node(node)
    return dag


def _doc(node_id: str, attributes: dict) -> Node:
    return Node(id=node_id, kind="design_doc", attributes=attributes)


def _run(*nodes: Node):
    return SemanticContractConflictCheck(
        dag=_dag(*nodes), project_root=None, settings={}
    ).run()


def _conflicts_of_type(result, conflict_type: str) -> list[dict]:
    return [w for w in result.warnings if w.get("type") == conflict_type]


def test_semantic_contract_conflict_registered():
    assert get_registry()["semantic_contract_conflict"] is SemanticContractConflictCheck


# Fixture 1 — same field, same policy, same value: pass, no conflicts.
def test_same_field_same_policy_same_value_passes():
    result = _run(
        _doc(
            "design.md",
            {
                "aggregation_policies": [
                    {"field_id": "items", "policy": "all"},
                    {"field_id": "items", "policy": "all"},
                ]
            },
        )
    )
    assert result.status == "pass"
    assert result.passed is True
    assert result.skipped is False
    assert result.block_deploy is False
    assert result.warnings == []
    assert result.checked_count >= 1


# Fixture 2 — same field, policy=all vs policy=representative: amber conflict.
def test_same_field_conflicting_policy_warns_amber():
    result = _run(
        _doc(
            "design.md",
            {
                "aggregation_policies": [
                    {"field_id": "items", "policy": "all"},
                    {"field_id": "items", "policy": "representative"},
                ]
            },
        )
    )
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False

    conflicts = _conflicts_of_type(result, "scalar_contract_conflict")
    assert len(conflicts) == 1
    entry = conflicts[0]
    assert entry["section"] == "aggregation_policies"
    assert entry["identity"] == "items"
    assert entry["key"] == "policy"
    assert set(entry["values"]) == {"all", "representative"}
    assert entry["severity"] == "amber"
    assert entry["remediation"]


# Fixture 3 — different fields with different policy values: pass (no conflict).
def test_different_field_does_not_conflict():
    result = _run(
        _doc(
            "design.md",
            {
                "aggregation_policies": [
                    {"field_id": "items", "policy": "all"},
                    {"field_id": "totals", "policy": "representative"},
                ]
            },
        )
    )
    assert result.status == "pass"
    assert result.passed is True
    assert result.warnings == []


# Fixture 4 — a free-text / non-scalar contradiction is out of scope: skip-style
# (no conflict). Same identity, same key, but the values are dict/list (not
# scalars) so they are never compared.
def test_free_text_contradiction_is_skipped():
    result = _run(
        _doc(
            "design.md",
            {
                "presentation_specs": [
                    {
                        "field_id": "summary",
                        # non-scalar values for the SAME key on the SAME identity:
                        # must not be compared (no general meaning-conflict).
                        "format": {"note": "must be fast"},
                    },
                    {
                        "field_id": "summary",
                        "format": {"note": "must be strict and exhaustive"},
                    },
                ]
            },
        )
    )
    assert result.passed is True
    assert _conflicts_of_type(result, "scalar_contract_conflict") == []


# Extra guard — no target sections at all: skip (dormant, false-red guard).
def test_no_target_sections_skips():
    result = _run(_doc("design.md", {"some_unrelated_attr": [1, 2, 3]}))
    assert result.skipped is True
    assert result.status == "skip"
    assert result.passed is True
    assert result.warnings == []
