"""Coverage for the cross_route_state_restore operational E2E axis.

The axis proves that declared client-side UI state survives a same-app
navigation round-trip (leave to a different in-app route and return), an
independent failure mode from durable reload readback. Fixtures use only
generic SPA vocabulary (draft / resume / restore) so the axis stays domain
agnostic and never encodes a single product's wording.
"""

from codd.e2e_extractor import ScenarioExtractor, UserScenario
from codd.operational_e2e_audit import _scenario_dod_obligations, build_operational_e2e_audit


def test_cross_route_state_restore_generated_for_client_route_with_navigation_contract(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: edit_draft
      actor: author
      verb: update
      target: article_draft
      route: /articles/new
      restore_on_return: unsaved draft content is restored when the author returns
      expected_outcomes:
        - draft content is saved
""",
        encoding="utf-8",
    )

    collection = ScenarioExtractor(tmp_path).extract_operational()
    restore = next(
        scenario
        for scenario in collection.scenarios
        if scenario.coverage_axis == "cross_route_state_restore"
    )

    assert restore.name == "author edit_draft cross route state restore"
    assert restore.actor == "author"
    assert restore.routes == ["/articles/new"]
    assert "cross_route_readback" in [item.id for item in restore.dod_obligations]
    assert any("client-side navigation round-trip" in item for item in restore.acceptance_criteria)
    assert any(
        "navigate away to a different in-app route" in step.lower() for step in restore.steps
    )
    assert "unsaved draft content is restored when the author returns" in restore.observable_outcomes


def test_cross_route_state_restore_not_generated_for_system_actor(tmp_path):
    # A non-interactive system actor declares a resume contract but has no human
    # client surface to navigate, so the gate must stay quiet (noise prevention).
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: rebuild_cache
      actor: system
      verb: update
      target: cache
      route: /internal/cache
      resume_state: cache resume checkpoint is restored on restart
      expected_outcomes: [cache is rebuilt]
""",
        encoding="utf-8",
    )

    collection = ScenarioExtractor(tmp_path).extract_operational()
    axes = {scenario.coverage_axis for scenario in collection.scenarios}

    assert "cross_route_state_restore" not in axes


def test_cross_route_state_restore_not_generated_for_plain_crud_readback(tmp_path):
    # A mutating operation with a durable readback but no navigation-persistence
    # contract must NOT inflate into a cross-route scenario; the two axes are
    # orthogonal and the gate stays conservative.
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: assign_item
      actor: operator
      verb: assign
      target: work_item
      route: /work-items
      expected_outcomes: [assignment persists]
""",
        encoding="utf-8",
    )

    collection = ScenarioExtractor(tmp_path).extract_operational()
    axes = {scenario.coverage_axis for scenario in collection.scenarios}

    assert "persistence_readback" in axes
    assert "cross_route_state_restore" not in axes


def test_audit_surfaces_cross_route_readback_obligation_as_uncovered(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: edit_draft
      actor: author
      verb: update
      target: article_draft
      route: /articles/new
      restore_on_return: unsaved draft content is restored when the author returns
      expected_outcomes:
        - draft content is saved
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)
    row = next(row for row in report.rows if row.coverage_axis == "cross_route_state_restore")

    assert row.coverage_status == "uncovered"
    assert any("cross_route_readback" in item for item in row.required_evidence)


def test_scenario_dod_obligations_fallback_includes_cross_route_readback():
    # When a scenario carries no inline obligations (for example, loaded from a
    # markdown catalog without a DoD section), the audit fallback must still
    # require the cross-route readback obligation.
    scenario = UserScenario(
        name="author edit_draft cross route state restore",
        steps=[],
        routes=["/articles/new"],
        acceptance_criteria=[],
        kind="operational",
        actor="author",
        coverage_axis="cross_route_state_restore",
        observable_outcomes=["unsaved draft content is restored on return"],
    )

    obligations = _scenario_dod_obligations(scenario)

    assert "cross_route_readback" in [item.id for item in obligations]
