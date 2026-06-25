"""Stage-2 Axis-P Phase C1: gap_kind -> contract_key routing table tests."""

from __future__ import annotations

from codd.elicit.routing import (
    DEFAULT_GAP_ROUTING,
    PROMOTION_SOURCE,
    resolve_routing,
    route_gap_kind,
    split_contract_key,
)


# ---------------------------------------------------------------------------
# RECOMMENDED default routing
# ---------------------------------------------------------------------------

def test_missing_journey_routes_to_user_journeys() -> None:
    assert route_gap_kind("missing_journey_for_actor") == ("user_journeys",)


def test_missing_producer_routes_to_resource_and_capability_contracts() -> None:
    assert route_gap_kind("missing_producer") == (
        "resource_contracts",
        "capability_contracts",
    )


def test_consumed_not_produced_routes_to_resource_and_capability_contracts() -> None:
    assert route_gap_kind("consumed_not_produced") == (
        "resource_contracts",
        "capability_contracts",
    )


def test_environment_and_variant_route_to_coverage_axes() -> None:
    assert route_gap_kind("environment_matrix") == ("coverage_axes",)
    assert route_gap_kind("variant_coverage") == ("coverage_axes",)


def test_forbidden_and_negative_space_route_to_forbidden_evidence() -> None:
    assert route_gap_kind("forbidden_pattern") == (
        "negative_space.forbidden_evidence",
    )
    assert route_gap_kind("negative_space_gap") == (
        "negative_space.forbidden_evidence",
    )


def test_cardinality_routes_to_aggregation_policies() -> None:
    assert route_gap_kind("cardinality_rule") == ("aggregation_policies",)


def test_acceptance_signal_and_e2e_route_to_expected_outcomes() -> None:
    assert route_gap_kind("acceptance_signal") == (
        "user_journeys.expected_outcomes",
    )
    assert route_gap_kind("e2e_outcome") == ("user_journeys.expected_outcomes",)


def test_nfr_routes_to_runtime_constraints() -> None:
    assert route_gap_kind("nfr_latency") == ("runtime_constraints",)


# ---------------------------------------------------------------------------
# unknown kind -> no routing (safe side)
# ---------------------------------------------------------------------------

def test_unknown_kind_routes_nowhere() -> None:
    assert route_gap_kind("some_brand_new_kind") == ()


def test_empty_or_none_kind_routes_nowhere() -> None:
    assert route_gap_kind("") == ()
    assert route_gap_kind(None) == ()


def test_canonicalization_matches_hyphen_and_case_variants() -> None:
    # gap kinds are canonicalized (lower + non-alnum -> _) before matching.
    assert route_gap_kind("Missing-Journey-For-Actor") == ("user_journeys",)
    assert route_gap_kind("MISSING_PRODUCER") == (
        "resource_contracts",
        "capability_contracts",
    )


# ---------------------------------------------------------------------------
# override (codd.yaml axis_p.gap_routing) — RECOMMENDED default is overridable
# ---------------------------------------------------------------------------

def test_config_override_repoints_existing_kind() -> None:
    config = {"axis_p": {"gap_routing": {"missing_journey*": ["runtime_constraints"]}}}
    assert route_gap_kind("missing_journey_for_actor", codd_config=config) == (
        "runtime_constraints",
    )


def test_config_override_adds_new_custom_kind() -> None:
    config = {"axis_p": {"gap_routing": {"my_custom_gap": ["resource_contracts"]}}}
    assert route_gap_kind("my_custom_gap", codd_config=config) == (
        "resource_contracts",
    )
    # default rules still apply for non-overridden kinds.
    assert route_gap_kind("nfr_latency", codd_config=config) == (
        "runtime_constraints",
    )


def test_config_override_can_disable_a_kind_with_empty_list() -> None:
    config = {"axis_p": {"gap_routing": {"nfr*": []}}}
    assert route_gap_kind("nfr_latency", codd_config=config) == ()


def test_call_override_takes_precedence_over_config() -> None:
    config = {"axis_p": {"gap_routing": {"nfr*": ["runtime_constraints"]}}}
    override = {"nfr*": ["coverage_axes"]}
    assert route_gap_kind("nfr_latency", codd_config=config, override=override) == (
        "coverage_axes",
    )


def test_resolve_routing_preserves_default_order_then_appends_overrides() -> None:
    rules = resolve_routing({"axis_p": {"gap_routing": {"zzz_custom": ["coverage_axes"]}}})
    patterns = [pattern for pattern, _ in rules]
    # default patterns appear first, in their declared order
    assert patterns[: len(DEFAULT_GAP_ROUTING)] == [p for p, _ in DEFAULT_GAP_ROUTING]
    # appended override last
    assert patterns[-1] == "zzz_custom"


# ---------------------------------------------------------------------------
# split_contract_key
# ---------------------------------------------------------------------------

def test_split_plain_key_has_no_sub_key() -> None:
    assert split_contract_key("resource_contracts") == ("resource_contracts", None)


def test_split_dotted_key_returns_top_and_sub() -> None:
    assert split_contract_key("user_journeys.expected_outcomes") == (
        "user_journeys",
        "expected_outcomes",
    )
    assert split_contract_key("negative_space.forbidden_evidence") == (
        "negative_space",
        "forbidden_evidence",
    )


def test_promotion_source_marker_is_stable() -> None:
    assert PROMOTION_SOURCE == "axis_p_confirmed"
