"""Tests for the resource_flow_coherence DAG check (consumed-but-never-produced)."""

from __future__ import annotations

from codd.dag import DAG, Node
from codd.dag.checks.resource_flow_coherence import ResourceFlowCoherenceCheck


def _dag(*nodes: Node) -> DAG:
    dag = DAG()
    for node in nodes:
        dag.add_node(node)
    return dag


def _design_doc(node_id: str = "docs/design/integration.md", **attributes) -> Node:
    return Node(id=node_id, kind="design_doc", attributes=attributes)


def _run(dag: DAG):
    return ResourceFlowCoherenceCheck(dag).run()


def _warnings_of_type(result, warning_type: str) -> list[dict]:
    return [warning for warning in result.warnings if warning["type"] == warning_type]


CRITICAL_JOURNEY = {
    "name": "line_individual_nudge_to_inactive_learners",
    "criticality": "critical",
    "steps": [{"action": "send_individual_line_nudge"}],
    "required_capabilities": ["line_individual_nudge", "lstep_tag_reflection"],
    "expected_outcome_refs": [],
}


# Fixture 1 — existing projects with no contracts keep passing.
def test_no_contracts_skips():
    dag = _dag(_design_doc(user_journeys=[CRITICAL_JOURNEY]))
    result = _run(dag)
    assert result.skipped is True
    assert result.passed is True
    assert result.status == "skip"
    assert result.warnings == []


# Fixture 1 — green control: produced and consumed resources are not dead.
def test_dead_resource_green_control_produced_and_consumed_has_no_warning():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
                {
                    "capability": "bind_line_friend_to_user",
                    "produces": [{"resource": "data:users.lstep_friend_id"}],
                },
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert _warnings_of_type(result, "dead_resource") == []


# Fixture 2 — false-green candidate: produced with no consumers is amber only.
def test_dead_resource_false_green_candidate_warns_amber_only():
    dag = _dag(
        _design_doc(
            capability_contracts=[
                {
                    "capability": "build_unused_export",
                    "produces": [{"resource": "data:exports.unused"}],
                }
            ],
        )
    )
    result = _run(dag)
    dead_resource_warnings = _warnings_of_type(result, "dead_resource")
    assert result.passed is True
    assert result.violations == []
    assert len(dead_resource_warnings) == 1
    assert dead_resource_warnings[0]["severity"] == "amber"
    assert dead_resource_warnings[0]["resource"] == "data:exports.unused"


# Fixture 3 — false-red guard: consumed-only and external resources are not dead.
def test_dead_resource_false_red_guard_ignores_consumed_only_and_external():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                }
            ],
            resource_contracts=[
                {
                    "resource": "data:vendor.seed_payload",
                    "externally_provided_by": [{"provider": "vendor_seed_file"}],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is False
    assert any(v["type"] == "dangling_required_consumer" for v in result.violations)
    assert _warnings_of_type(result, "dead_resource") == []


# Fixture 4 — legacy backcompat: no contracts remain a clean skip.
def test_dead_resource_legacy_backcompat_no_contracts_stays_quiet():
    dag = _dag(_design_doc(user_journeys=[CRITICAL_JOURNEY]))
    result = _run(dag)
    assert result.skipped is True
    assert result.passed is True
    assert result.status == "skip"
    assert result.warnings == []


# Fixture 2 — required consumer in a critical journey with no producer → RED.
def test_required_consumer_without_producer_is_red():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                    "produces": [],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is False
    assert result.severity == "red"
    assert any(
        v["type"] == "dangling_required_consumer"
        and v["resource"] == "data:users.lstep_friend_id"
        and v["consumer_capability"] == "line_individual_nudge"
        for v in result.violations
    )


# Fixture 3 — add a producer obligation → GREEN.
def test_required_consumer_with_producer_passes():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
                {
                    "capability": "bind_line_friend_to_user",
                    "produces": [{"resource": "data:users.lstep_friend_id"}],
                },
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert result.violations == []


# Fixture 4a — optional consumer (required: false / on_missing: skip) → not gated.
def test_optional_consumer_passes():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": False,
                            "on_missing": "skip",
                        }
                    ],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert result.violations == []


# Fixture 4b — externally provided resource satisfies the consumer.
def test_external_provider_satisfies_consumer():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                }
            ],
            resource_contracts=[
                {
                    "resource": "data:users.lstep_friend_id",
                    "externally_provided_by": [{"provider": "lstep_friend_webhook"}],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True


# A required read whose capability is not on a critical journey is advisory, not red.
def test_required_read_off_critical_journey_is_warning():
    dag = _dag(
        _design_doc(
            user_journeys=[
                {
                    "name": "low_priority",
                    "criticality": "low",
                    "required_capabilities": ["line_individual_nudge"],
                    "steps": [],
                    "expected_outcome_refs": [],
                }
            ],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert any(w["type"] == "unscoped_resource_consumer" for w in result.warnings)


# Regression — the real osato false-green: per-person LINE nudge + tag reflection
# both read lstep_friend_id, the webhook never writes it, no producer declared → RED.
def test_osato_lstep_friend_id_regression():
    dag = _dag(
        _design_doc(
            node_id="docs/design/integration_design.md",
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                            "reason": "LINE個別送信の宛先解決に必要",
                        }
                    ],
                },
                {
                    "capability": "lstep_tag_reflection",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
            ],
        )
    )
    result = _run(dag)
    assert result.passed is False
    assert result.severity == "red"
    assert {v["resource"] for v in result.violations} == {"data:users.lstep_friend_id"}
    assert {v["consumer_capability"] for v in result.violations} == {
        "line_individual_nudge",
        "lstep_tag_reflection",
    }


# ── malformed_contract (Tier-1 of extractor_silent_noop): declared-but-unusable ──

# candidate: a consume declared without its required `resource` is surfaced, not dropped.
def test_malformed_contract_entry_is_amber():
    dag = _dag(
        _design_doc(
            capability_contracts=[
                {"capability": "line_individual_nudge", "consumes": [{"required": True}]},
            ],
        )
    )
    result = _run(dag)
    assert any(w["type"] == "malformed_contract" for w in result.warnings)


# guard: a well-formed contract emits no malformed_contract warning.
def test_wellformed_contract_no_malformed_warning():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "bind_line_friend_to_user",
                    "produces": [{"resource": "data:users.lstep_friend_id"}],
                },
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
            ],
        )
    )
    result = _run(dag)
    assert not any(w["type"] == "malformed_contract" for w in result.warnings)


# legacy: no contracts at all → skip, no malformed_contract warning.
def test_malformed_contract_skip_no_contracts():
    dag = _dag(_design_doc(user_journeys=[CRITICAL_JOURNEY]))
    result = _run(dag)
    assert result.skipped is True
    assert not any(w["type"] == "malformed_contract" for w in result.warnings)


# ── diagnostic_incompleteness: every red carries an actionable remediation ──
def test_dangling_consumer_violation_has_remediation():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.violations
    assert all(v.get("remediation") for v in result.violations)


# every amber finding is self-repairable too (dead_resource shown; malformed/unscoped likewise).
def test_dead_resource_warning_has_remediation():
    dag = _dag(
        _design_doc(
            capability_contracts=[
                {"capability": "build_report", "produces": [{"resource": "data:report.cache"}]},
            ],
        )
    )
    result = _run(dag)
    dead = [w for w in result.warnings if w["type"] == "dead_resource"]
    assert dead and all(w.get("remediation") for w in dead)


# ── producer_after_consumer (explicit operation ordering, opt-in) ──────────────
#
# An operation_flow declares ordered operations; the ordering check fires ONLY
# when a design opted into it. Each operation here carries a `capability` ref so
# producer obligations / consumer capabilities map to a single operation index.

_PRODUCER_CONSUMER_JOURNEY = {
    "name": "issue_then_redeem_token",
    "criticality": "critical",
    "steps": [{"action": "redeem_token"}],
    "required_capabilities": ["redeem_token"],
    "expected_outcome_refs": [],
}


def _ordering_doc(*, producer_index: int, consumer_index: int):
    """Design doc with an explicit operation_flow ordering producer vs consumer.

    The slot at ``producer_index`` is the resource producer, the slot at
    ``consumer_index`` is the required consumer on a critical journey.
    """

    slots = {
        producer_index: {"id": "issue_token", "capability": "issue_token"},
        consumer_index: {"id": "redeem_token", "capability": "redeem_token"},
    }
    operations = [slots[i] for i in sorted(slots)]
    return _design_doc(
        user_journeys=[_PRODUCER_CONSUMER_JOURNEY],
        operation_flow={"operations": operations},
        capability_contracts=[
            {
                "capability": "issue_token",
                "produces": [{"resource": "data:tokens.value"}],
            },
            {
                "capability": "redeem_token",
                "consumes": [
                    {
                        "resource": "data:tokens.value",
                        "required": True,
                        "on_missing": "fail",
                    }
                ],
            },
        ],
    )


# Fixture A — producer op index 0, consumer op index 1 → in order → pass.
def test_producer_before_consumer_passes():
    dag = _dag(_ordering_doc(producer_index=0, consumer_index=1))
    result = _run(dag)
    assert result.passed is True
    assert result.violations == []
    assert _warnings_of_type(result, "producer_after_consumer") == []


# Fixture B — consumer op index 0, producer op index 1 → consumed-before-produced → RED.
def test_consumer_before_producer_is_red():
    dag = _dag(_ordering_doc(producer_index=1, consumer_index=0))
    result = _run(dag)
    assert result.passed is False
    assert result.severity == "red"
    assert any(
        v["type"] == "producer_after_consumer"
        and v["resource"] == "data:tokens.value"
        and v["consumer_capability"] == "redeem_token"
        for v in result.violations
    )
    # The existence-based dangling red must NOT fire — a producer does exist.
    assert not any(v["type"] == "dangling_required_consumer" for v in result.violations)


# ── class resource_flow_operation_scope_false_red ──────────────────────────
# Operation order is meaningful only WITHIN one explicit flow. A producer in a
# different doc's independent flow has no ordering relation to this consumer; a
# global concatenated index would red it purely on doc-sort order (a false-red).
def _independent_flow_docs(consumer_first: bool):
    consumer_doc = _design_doc(
        node_id="docs/design/a_consumer.md",
        user_journeys=[_PRODUCER_CONSUMER_JOURNEY],
        operation_flow={"operations": [{"id": "redeem_token", "capability": "redeem_token"}]},
        capability_contracts=[
            {
                "capability": "redeem_token",
                "consumes": [
                    {"resource": "data:tokens.value", "required": True, "on_missing": "fail"}
                ],
            }
        ],
    )
    producer_doc = _design_doc(
        node_id="docs/design/z_producer.md",
        operation_flow={"operations": [{"id": "issue_token", "capability": "issue_token"}]},
        capability_contracts=[
            {"capability": "issue_token", "produces": [{"resource": "data:tokens.value"}]}
        ],
    )
    order = (consumer_doc, producer_doc) if consumer_first else (producer_doc, consumer_doc)
    return _dag(*order)


# false_red_guard — independent flows in separate docs must NOT red on doc order.
def test_independent_flows_across_docs_do_not_false_red():
    result = _run(_independent_flow_docs(consumer_first=True))
    assert _warnings_of_type(result, "producer_after_consumer") == []
    assert not any(v.get("type") == "producer_after_consumer" for v in result.violations)
    # Existence is satisfied cross-doc, so no dangling red either.
    assert not any(v.get("type") == "dangling_required_consumer" for v in result.violations)


# held-out (metamorphic) — the verdict must be invariant to doc insertion order.
def test_independent_flows_verdict_is_order_invariant():
    red_when_consumer_first = any(
        v.get("type") == "producer_after_consumer"
        for v in _run(_independent_flow_docs(consumer_first=True)).violations
    )
    red_when_producer_first = any(
        v.get("type") == "producer_after_consumer"
        for v in _run(_independent_flow_docs(consumer_first=False)).violations
    )
    assert red_when_consumer_first is False
    assert red_when_producer_first is False


# ── class resource_flow_ambiguous_alias_false_red ──────────────────────────
# An alias that resolves to >1 canonical resource is left un-canonicalized; a
# consumer using it must NOT red as dangling (a producer exists for a target).
def test_ambiguous_alias_consumer_does_not_false_red():
    dag = _dag(
        _design_doc(
            user_journeys=[
                {
                    "name": "use_user_flow",
                    "criticality": "critical",
                    "steps": [{"action": "use_user"}],
                    "required_capabilities": ["use_user_id"],
                    "expected_outcome_refs": [],
                }
            ],
            resource_contracts=[
                {"resource": "data:users.id", "aliases": ["user_id"]},
                {"resource": "data:accounts.id", "aliases": ["user_id"]},
            ],
            capability_contracts=[
                {"capability": "make_user", "produces": [{"resource": "data:users.id"}]},
                {
                    "capability": "use_user_id",
                    "consumes": [
                        {"resource": "user_id", "required": True, "on_missing": "fail"}
                    ],
                },
            ],
        )
    )
    result = _run(dag)
    # The ambiguous alias must not manufacture a dangling false-red...
    assert not any(
        v.get("type") == "dangling_required_consumer" for v in result.violations
    )
    # ...it is surfaced as amber instead.
    assert _warnings_of_type(result, "ambiguous_alias_unresolved")


# Fixture C — contract present but operations carry no matching refs → ordering skipped → pass.
def test_contract_without_operation_refs_passes():
    dag = _dag(
        _design_doc(
            user_journeys=[_PRODUCER_CONSUMER_JOURNEY],
            # operations exist but reference unrelated ids → producer/consumer
            # cannot be mapped to any operation index → no ordering, no red.
            operation_flow={
                "operations": [
                    {"id": "unrelated_a"},
                    {"id": "unrelated_b"},
                ]
            },
            capability_contracts=[
                {
                    "capability": "issue_token",
                    "produces": [{"resource": "data:tokens.value"}],
                },
                {
                    "capability": "redeem_token",
                    "consumes": [
                        {
                            "resource": "data:tokens.value",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert result.violations == []
    assert _warnings_of_type(result, "producer_after_consumer") == []


# Fixture D — external provider + consumer ordered before the internal producer → pass.
# External providers are out of scope for ordering; an external source pre-exists
# the flow, so a consumer "before" the internal producer must not be a red.
def test_external_provider_consumer_before_internal_producer_passes():
    dag = _dag(
        _design_doc(
            user_journeys=[_PRODUCER_CONSUMER_JOURNEY],
            operation_flow={
                "operations": [
                    {"id": "redeem_token", "capability": "redeem_token"},
                    {"id": "issue_token", "capability": "issue_token"},
                ]
            },
            capability_contracts=[
                {
                    "capability": "issue_token",
                    "produces": [{"resource": "data:tokens.value"}],
                },
                {
                    "capability": "redeem_token",
                    "consumes": [
                        {
                            "resource": "data:tokens.value",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
            ],
            resource_contracts=[
                {
                    "resource": "data:tokens.value",
                    "externally_provided_by": [{"provider": "token_seed_file"}],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert result.violations == []
    assert _warnings_of_type(result, "producer_after_consumer") == []


# ── alias drift (duplicate_alias_target / alias_shadows_canonical) ─────────────
#
# Aliases are exact-string only — no fuzzy / case-insensitive normalization. An
# alias resolving to exactly one canonical resource is normal (no warning). Two
# minimal incoherence shapes are surfaced amber (never red):
#   * duplicate_alias_target  — the same alias name maps to >1 canonical resource.
#   * alias_shadows_canonical — an alias name is also a canonical resource of
#                               another entry.


# Fixture A — consumer uses an alias that resolves to a single canonical → pass, no warning.
def test_alias_resolves_to_single_canonical_no_warning():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "user_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
                {
                    "capability": "bind_line_friend_to_user",
                    "produces": [{"resource": "data:users.id"}],
                },
            ],
            resource_contracts=[
                {"resource": "data:users.id", "aliases": ["user_id"]},
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert _warnings_of_type(result, "duplicate_alias_target") == []
    assert _warnings_of_type(result, "alias_shadows_canonical") == []


# Fixture B — same alias name maps to two different canonicals → duplicate_alias_target amber.
def test_duplicate_alias_target_is_amber():
    dag = _dag(
        _design_doc(
            resource_contracts=[
                {"resource": "data:users.id", "aliases": ["user_id"]},
                {"resource": "data:accounts.id", "aliases": ["user_id"]},
            ],
        )
    )
    result = _run(dag)
    dup = _warnings_of_type(result, "duplicate_alias_target")
    assert result.passed is True
    assert result.violations == []
    assert len(dup) == 1
    assert dup[0]["severity"] == "amber"
    assert dup[0]["alias"] == "user_id"
    assert set(dup[0]["canonical_resources"]) == {"data:users.id", "data:accounts.id"}


# Fixture C — an alias name is also a canonical resource of another entry → alias_shadows_canonical amber.
def test_alias_shadows_canonical_is_amber():
    dag = _dag(
        _design_doc(
            resource_contracts=[
                {"resource": "data:users.id", "aliases": ["user"]},
                {"resource": "user", "aliases": []},
            ],
        )
    )
    result = _run(dag)
    shadow = _warnings_of_type(result, "alias_shadows_canonical")
    assert result.passed is True
    assert result.violations == []
    assert len(shadow) == 1
    assert shadow[0]["severity"] == "amber"
    assert shadow[0]["alias"] == "user"


# Fixture D — look-alike names (exact-string differ) are NOT fuzzy-matched → no warning.
def test_lookalike_aliases_are_not_fuzzy_matched():
    dag = _dag(
        _design_doc(
            resource_contracts=[
                {"resource": "data:users.id", "aliases": ["user_id"]},
                {"resource": "data:accounts.id", "aliases": ["userID"]},
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert _warnings_of_type(result, "duplicate_alias_target") == []
    assert _warnings_of_type(result, "alias_shadows_canonical") == []


# transparency: a PASS reports how many resource uses it actually checked.
def test_pass_message_reports_checked_count():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "bind_line_friend_to_user",
                    "produces": [{"resource": "data:users.lstep_friend_id"}],
                },
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert "checked" in result.message


# ── Fix 1: contracts/journeys nested under frontmatter / frontmatter.codd ───────
#
# The generator stashes contract + journey metadata at the canonical
# ``frontmatter.codd`` location; some builders surface it at ``frontmatter`` or
# the top level. resource_flow_coherence must read all three (mirroring
# semantic_contract_conflict._section_entries) or it silently skips real
# incompleteness declared in the canonical place (a false-green).


# Fix 1a — dangling required consumer declared under frontmatter.codd → RED (was: skip).
def test_frontmatter_codd_dangling_required_consumer_is_red():
    dag = _dag(
        _design_doc(
            frontmatter={
                "codd": {
                    "user_journeys": [CRITICAL_JOURNEY],
                    "capability_contracts": [
                        {
                            "capability": "line_individual_nudge",
                            "consumes": [
                                {
                                    "resource": "data:users.lstep_friend_id",
                                    "required": True,
                                    "on_missing": "fail",
                                }
                            ],
                        }
                    ],
                }
            }
        )
    )
    result = _run(dag)
    assert result.skipped is False
    assert result.passed is False
    assert result.severity == "red"
    assert any(
        v["type"] == "dangling_required_consumer"
        and v["resource"] == "data:users.lstep_friend_id"
        and v["consumer_capability"] == "line_individual_nudge"
        for v in result.violations
    )


# Fix 1b — same shape declared one level up at frontmatter (not under codd) → RED.
def test_frontmatter_direct_dangling_required_consumer_is_red():
    dag = _dag(
        _design_doc(
            frontmatter={
                "user_journeys": [CRITICAL_JOURNEY],
                "capability_contracts": [
                    {
                        "capability": "line_individual_nudge",
                        "consumes": [
                            {
                                "resource": "data:users.lstep_friend_id",
                                "required": True,
                                "on_missing": "fail",
                            }
                        ],
                    }
                ],
            }
        )
    )
    result = _run(dag)
    assert result.skipped is False
    assert result.passed is False
    assert result.severity == "red"
    assert any(
        v["type"] == "dangling_required_consumer"
        and v["resource"] == "data:users.lstep_friend_id"
        for v in result.violations
    )


# Fix 1c — regression: top-level declaration still detects exactly as before.
def test_top_level_dangling_required_consumer_still_red_regression():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is False
    assert result.severity == "red"
    assert any(
        v["type"] == "dangling_required_consumer"
        and v["resource"] == "data:users.lstep_friend_id"
        for v in result.violations
    )


# Fix 1 — journey criticality must also be read from frontmatter.codd: a producer
# declared top-level but the critical journey + required consumer under
# frontmatter.codd must still gate (journey read from the same three locations).
def test_frontmatter_codd_journey_scopes_consumer_red():
    dag = _dag(
        _design_doc(
            # journey lives under frontmatter.codd; consumer lives top-level.
            frontmatter={"codd": {"user_journeys": [CRITICAL_JOURNEY]}},
            capability_contracts=[
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                }
            ],
        )
    )
    result = _run(dag)
    # If the journey were NOT read from frontmatter.codd, line_individual_nudge
    # would not be a critical-journey capability and this would be an amber
    # unscoped_resource_consumer (false-green) instead of a red.
    assert result.passed is False
    assert result.severity == "red"
    assert any(
        v["type"] == "dangling_required_consumer"
        and v["consumer_capability"] == "line_individual_nudge"
        for v in result.violations
    )


# ── Fix 2: no-violation return must be amber/warn when warnings are present ─────
#
# A producer-only contract emits a dead_resource amber warning. The no-violation
# branch previously returned severity="info"/status="pass", so the CLI (which
# only renders WARN for severity=="amber") hid the finding behind a PASS row and
# never counted it. With warnings present the result must be amber/warn (deploy
# still allowed: passed=True, block_deploy=False).


# Fix 2a — producer-only (dead_resource warning) → amber/warn, deploy still allowed.
def test_no_violation_with_warnings_is_amber_warn():
    dag = _dag(
        _design_doc(
            capability_contracts=[
                {
                    "capability": "build_unused_export",
                    "produces": [{"resource": "data:exports.unused"}],
                }
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert result.violations == []
    assert _warnings_of_type(result, "dead_resource")
    # the bug: these were "info"/"pass" with a finding present.
    assert result.severity == "amber"
    assert result.status == "warn"
    assert result.block_deploy is False


# Fix 2b — guard: a clean pass with NO warnings stays info/pass (unchanged).
def test_no_violation_without_warnings_stays_info_pass():
    dag = _dag(
        _design_doc(
            user_journeys=[CRITICAL_JOURNEY],
            capability_contracts=[
                {
                    "capability": "bind_line_friend_to_user",
                    "produces": [{"resource": "data:users.lstep_friend_id"}],
                },
                {
                    "capability": "line_individual_nudge",
                    "consumes": [
                        {
                            "resource": "data:users.lstep_friend_id",
                            "required": True,
                            "on_missing": "fail",
                        }
                    ],
                },
            ],
        )
    )
    result = _run(dag)
    assert result.passed is True
    assert result.violations == []
    assert result.warnings == []
    assert result.severity == "info"
    assert result.status == "pass"
