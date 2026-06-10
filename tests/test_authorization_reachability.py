"""Tests for the grant verb class, authorization reachability advisory (T4)
and the enables-declaration doctor nudge (T5).

All flows are synthetic and framework-agnostic (operator/member/reviewer +
workspace/record vocabulary); no project-specific names.
"""

from __future__ import annotations

from codd.capability_completeness import (
    DEFAULT_CONSUME_VERBS,
    DEFAULT_GRANT_VERBS,
    DEFAULT_PRODUCE_VERBS,
    capability_completeness_warnings,
    detect_authorization_reachability_gaps,
    detect_capability_gaps,
    enablement_declaration_nudges,
    grant_verb_settings,
)


def _flow(*operations: dict, **flow_extra) -> dict:
    return {"operations": list(operations), **flow_extra}


# --- grant verb class ---------------------------------------------------------


def test_grant_verbs_do_not_overlap_existing_classes() -> None:
    assert not (DEFAULT_GRANT_VERBS & DEFAULT_PRODUCE_VERBS)
    assert not (DEFAULT_GRANT_VERBS & DEFAULT_CONSUME_VERBS)


def test_grant_verbs_extendable_via_config() -> None:
    verbs = grant_verb_settings({"capability_completeness": {"grant_verbs": ["bestow"]}})
    assert "bestow" in verbs
    assert DEFAULT_GRANT_VERBS.issubset(verbs)


def test_grant_verbs_default_without_config() -> None:
    assert grant_verb_settings({}) == DEFAULT_GRANT_VERBS


# --- authorization reachability advisory --------------------------------------


def test_grant_without_enables_and_consumed_target_is_reported() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "create_workspace", "verb": "create", "target": "workspace", "actor": "operator"},
                {"id": "share_workspace", "verb": "share", "target": "workspace", "actor": "operator"},
                {"id": "open_workspace", "verb": "open", "target": "workspace", "actor": "member"},
            ),
        )
    ]

    gaps = detect_authorization_reachability_gaps(flows)

    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.grant_operation == "share_workspace"
    assert gap.target == "workspace"
    assert gap.consumers == ("open_workspace",)
    assert gap.consumer_actors == ("member",)
    assert "authorization_reachability" in gap.message
    assert "`enables`" in gap.message
    assert "enablement_chain" in gap.message


def test_grant_with_enables_declared_is_wired_no_gap() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {
                    "id": "share_workspace",
                    "verb": "share",
                    "target": "workspace",
                    "actor": "operator",
                    "enables": [{"actor": "member", "operations": ["open_workspace"]}],
                },
                {"id": "open_workspace", "verb": "open", "target": "workspace", "actor": "member"},
            ),
        )
    ]

    assert detect_authorization_reachability_gaps(flows) == ()


def test_grant_without_consumers_is_not_reported() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "share_workspace", "verb": "share", "target": "workspace", "actor": "operator"},
            ),
        )
    ]

    assert detect_authorization_reachability_gaps(flows) == ()


def test_grant_advisory_appears_in_capability_warnings() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "assign_record", "verb": "assign", "target": "record", "actor": "operator"},
                {"id": "view_record", "verb": "view", "target": "record", "actor": "member"},
            ),
        )
    ]

    warnings = capability_completeness_warnings(flows, {})
    assert any("authorization_reachability" in warning for warning in warnings)


def test_grant_advisory_respects_module_enabled_flag() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "assign_record", "verb": "assign", "target": "record", "actor": "operator"},
                {"id": "view_record", "verb": "view", "target": "record", "actor": "member"},
            ),
        )
    ]

    assert capability_completeness_warnings(flows, {"capability_completeness": {"enabled": False}}) == []


def test_existing_capability_gap_computation_unchanged_by_grant_class() -> None:
    """F6 regression: grant verbs stay unclassified for produce/consume gaps."""
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "assign_record", "verb": "assign", "target": "record", "actor": "operator"},
                {"id": "view_record", "verb": "view", "target": "record", "actor": "member"},
            ),
        )
    ]

    gaps = detect_capability_gaps(flows)
    # 'assign' is not a produce verb: the consumed-without-producer gap remains.
    assert len(gaps) == 1
    assert gaps[0].target == "record"


def test_grant_verb_compound_token_matches() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "bulk_assign", "verb": "bulk_assign", "target": "record", "actor": "operator"},
                {"id": "view_record", "verb": "view", "target": "record", "actor": "member"},
            ),
        )
    ]

    gaps = detect_authorization_reachability_gaps(flows)
    assert len(gaps) == 1
    assert gaps[0].grant_operation == "bulk_assign"


# --- enables-declaration doctor nudge (T5) -------------------------------------


def test_nudge_when_outcome_mentions_other_actor_without_enables() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {
                    "id": "share_workspace",
                    "verb": "share",
                    "target": "workspace",
                    "actor": "operator",
                    "expected_outcomes": ["member in the target group can open the workspace"],
                },
                {"id": "open_workspace", "verb": "open", "target": "workspace", "actor": "member"},
            ),
        )
    ]

    nudges = enablement_declaration_nudges(flows, {})
    assert len(nudges) == 1
    assert "share_workspace" in nudges[0]
    assert "'member'" in nudges[0]
    assert "enables_nudge" in nudges[0]


def test_nudge_when_visible_to_names_other_actor() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {
                    "id": "close_item",
                    "verb": "complete",
                    "target": "work_item",
                    "actor": "operator",
                    "visible_to": "reviewer",
                },
                {"id": "review_item", "verb": "view", "target": "work_item", "actor": "reviewer"},
            ),
        )
    ]

    nudges = enablement_declaration_nudges(flows, {})
    assert len(nudges) == 1
    assert "'reviewer'" in nudges[0]


def test_no_nudge_when_enables_already_declared() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {
                    "id": "share_workspace",
                    "verb": "share",
                    "target": "workspace",
                    "actor": "operator",
                    "expected_outcomes": ["member can open the workspace"],
                    "enables": [{"actor": "member", "operations": ["open_workspace"]}],
                },
                {"id": "open_workspace", "verb": "open", "target": "workspace", "actor": "member"},
            ),
        )
    ]

    assert enablement_declaration_nudges(flows, {}) == []


def test_no_nudge_for_substring_actor_mention_false_positive() -> None:
    """'user' inside 'username' must not trigger the nudge (suppression)."""
    flows = [
        (
            "synthetic",
            _flow(
                {
                    "id": "update_profile",
                    "verb": "update",
                    "target": "profile",
                    "actor": "operator",
                    "expected_outcomes": ["the username is shown on the profile page"],
                },
                {"id": "view_profile", "verb": "view", "target": "profile", "actor": "user"},
            ),
        )
    ]

    assert enablement_declaration_nudges(flows, {}) == []


def test_no_nudge_when_only_own_actor_is_mentioned() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {
                    "id": "save_draft",
                    "verb": "save",
                    "target": "draft",
                    "actor": "operator",
                    "expected_outcomes": ["operator sees the saved draft"],
                },
                {"id": "view_draft", "verb": "view", "target": "draft", "actor": "member"},
            ),
        )
    ]

    assert enablement_declaration_nudges(flows, {}) == []


def test_nudge_matches_multiword_actor_names() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {
                    "id": "publish_report",
                    "verb": "publish",
                    "target": "report",
                    "actor": "operator",
                    "expected_outcomes": ["the central admin can download the report"],
                },
                {"id": "download_report", "verb": "download", "target": "report", "actor": "central_admin"},
            ),
        )
    ]

    nudges = enablement_declaration_nudges(flows, {})
    assert len(nudges) == 1
    assert "central_admin" in nudges[0]


def test_nudge_opt_out_via_config() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {
                    "id": "share_workspace",
                    "verb": "share",
                    "target": "workspace",
                    "actor": "operator",
                    "expected_outcomes": ["member can open the workspace"],
                },
                {"id": "open_workspace", "verb": "open", "target": "workspace", "actor": "member"},
            ),
        )
    ]

    assert enablement_declaration_nudges(flows, {"enables_nudge": {"enabled": False}}) == []


def test_nudge_silent_on_single_actor_flow() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {
                    "id": "save_draft",
                    "verb": "save",
                    "target": "draft",
                    "actor": "operator",
                    "expected_outcomes": ["operator sees the saved draft"],
                },
            ),
        )
    ]

    assert enablement_declaration_nudges(flows, {}) == []


def test_doctor_surfaces_nudge(tmp_path) -> None:
    from click.testing import CliRunner

    from codd.cli import main

    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: share_workspace
      actor: operator
      verb: share
      target: workspace
      expected_outcomes:
        - member can open the shared workspace
    - id: open_workspace
      actor: member
      verb: open
      target: workspace
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "enables_nudge" in result.output
    assert "share_workspace" in result.output
