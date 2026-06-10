"""Tests for the `enables` relationship and its coverage axes.

Generic synthetic flows only (operator/member/work_item vocabulary); no
project-specific names. Verifies:

- declared `enables` derives enablement_chain + access_path_variation:<path>
  variants + the access_path_variation:unrelated negative,
- undeclared flows derive *exactly* the same scenario set as before (F6),
- the normalization helper is tolerant of malformed declarations.
"""

from __future__ import annotations

import warnings as warnings_module
from dataclasses import asdict

import pytest

from codd.e2e_extractor import ScenarioExtractor
from codd.requirements_meta import operation_enables, normalize_operation_flow


_BASE_FLOW = """\
operation_flow:
  actors: [operator, member]
  operations:
    - id: share_workspace
      actor: operator
      verb: assign
      target: workspace
      route: /workspaces
      expected_outcomes:
        - member in the target group can open the shared workspace
{enables_block}\
    - id: open_workspace
      actor: member
      verb: open
      target: workspace
      route: /workspaces/:id
      expected_outcomes: [workspace content is visible]
"""

_ENABLES_BLOCK = """\
      enables:
        - actor: member
          operations: [open_workspace]
          access_paths: [granted, direct]
"""


def _write_flow(tmp_path, *, with_enables: bool) -> None:
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir(exist_ok=True)
    (codd_dir / "codd.yaml").write_text(
        _BASE_FLOW.format(enables_block=_ENABLES_BLOCK if with_enables else ""),
        encoding="utf-8",
    )


def test_enables_derives_enablement_chain(tmp_path):
    _write_flow(tmp_path, with_enables=True)
    collection = ScenarioExtractor(tmp_path).extract_operational()

    chain = [s for s in collection.scenarios if s.coverage_axis == "enablement_chain"]
    assert len(chain) == 1
    scenario = chain[0]
    assert scenario.actor == "member"
    assert scenario.operation_id == "open_workspace"
    # Structural injection of the grant completion as a precondition.
    assert any("operator has completed share_workspace" in p for p in scenario.preconditions)
    # The chain forbids direct-ownership fixture shortcuts.
    assert any("no direct-ownership fixture shortcut" in p for p in scenario.preconditions)
    # The enabled operation's own metadata is resolved (routes/outcomes).
    assert scenario.routes == ["/workspaces/:id"]
    assert "workspace content is visible" in scenario.observable_outcomes
    # Capability exercise, not observation.
    assert any("not merely as something the enabled actor observes" in a for a in scenario.acceptance_criteria)
    # Machine-checkable DoD obligation present.
    assert any(o.id == "enablement_exercise" for o in scenario.dod_obligations)


def test_enables_derives_access_path_variants_and_negative(tmp_path):
    _write_flow(tmp_path, with_enables=True)
    collection = ScenarioExtractor(tmp_path).extract_operational()

    axes = {s.coverage_axis for s in collection.scenarios}
    assert "access_path_variation:granted" in axes
    assert "access_path_variation:direct" in axes
    assert "access_path_variation:unrelated" in axes

    granted = next(s for s in collection.scenarios if s.coverage_axis == "access_path_variation:granted")
    assert granted.actor == "member"
    assert any("'granted' access path only" in p for p in granted.preconditions)
    assert any(o.id == "access_path_isolation" for o in granted.dod_obligations)

    negative = next(s for s in collection.scenarios if s.coverage_axis == "access_path_variation:unrelated")
    assert negative.operation_id == "open_workspace"
    assert any("cannot complete open_workspace" in a for a in negative.acceptance_criteria)
    assert any("no persisted state change" in a for a in negative.acceptance_criteria)
    assert any(o.id == "no_forbidden_mutation" for o in negative.dod_obligations)


def test_default_access_path_is_granted(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: link_record
      actor: operator
      verb: link
      target: record
      enables:
        - actor: member
          operations: [view_record]
    - id: view_record
      actor: member
      verb: view
      target: record
""",
        encoding="utf-8",
    )
    collection = ScenarioExtractor(tmp_path).extract_operational()
    axes = {s.coverage_axis for s in collection.scenarios}
    assert "access_path_variation:granted" in axes
    assert "access_path_variation:direct" not in axes
    assert "access_path_variation:unrelated" in axes


def test_undeclared_flow_scenario_set_is_unchanged(tmp_path):
    """F6 proof: removing the enables declaration reproduces the legacy set exactly.

    The with-enables run must equal the without-enables run **plus only**
    new-axis scenarios; every pre-existing scenario must be byte-identical.
    """

    base_dir = tmp_path / "without"
    base_dir.mkdir()
    _write_flow(base_dir, with_enables=False)
    enabled_dir = tmp_path / "with"
    enabled_dir.mkdir()
    _write_flow(enabled_dir, with_enables=True)

    without = ScenarioExtractor(base_dir).extract_operational().scenarios
    with_enables = ScenarioExtractor(enabled_dir).extract_operational().scenarios

    new_axes_prefixes = ("enablement_chain", "access_path_variation")
    legacy_from_enabled = [
        s for s in with_enables if not str(s.coverage_axis or "").startswith(new_axes_prefixes)
    ]

    assert [asdict(s) for s in legacy_from_enabled] == [asdict(s) for s in without]
    # And the new axes are strictly additive.
    assert len(with_enables) > len(without)


def test_undeclared_flow_has_no_new_axes(tmp_path):
    _write_flow(tmp_path, with_enables=False)
    collection = ScenarioExtractor(tmp_path).extract_operational()
    for scenario in collection.scenarios:
        axis = str(scenario.coverage_axis or "")
        assert axis != "enablement_chain"
        assert not axis.startswith("access_path_variation")


def test_unresolved_enabled_operation_still_derives_obligation(tmp_path):
    """A declared causal link to an id outside the flow still derives scenarios."""
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: grant_access
      actor: operator
      verb: grant
      target: resource
      enables:
        - actor: member
          operations: [external_consume]
""",
        encoding="utf-8",
    )
    collection = ScenarioExtractor(tmp_path).extract_operational()
    chain = [s for s in collection.scenarios if s.coverage_axis == "enablement_chain"]
    assert len(chain) == 1
    assert chain[0].operation_id == "external_consume"


def test_operation_enables_normalization():
    entries = operation_enables(
        {
            "id": "grant_access",
            "enables": [
                {"actor": "member", "operations": ["op_a", "op_b"]},
                {"actor": "guest", "operations": "op_c", "access_paths": ["direct"]},
            ],
        }
    )
    assert entries == [
        {"actor": "member", "operations": ["op_a", "op_b"], "access_paths": ["granted"]},
        {"actor": "guest", "operations": ["op_c"], "access_paths": ["direct"]},
    ]


def test_operation_enables_absent_returns_empty():
    assert operation_enables({"id": "x"}) == []
    assert operation_enables({"id": "x", "enables": None}) == []
    assert operation_enables(None) == []


def test_operation_enables_malformed_entries_warn_and_drop():
    with pytest.warns(UserWarning):
        entries = operation_enables(
            {"id": "x", "enables": [{"operations": ["op_a"]}, "garbage"]}
        )
    assert entries == []

    with pytest.warns(UserWarning):
        assert operation_enables({"id": "x", "enables": "not-a-list"}) == []


def test_operation_enables_strict_raises():
    with pytest.raises(ValueError):
        operation_enables({"id": "x", "enables": "broken"}, strict=True)


def test_normalize_operation_flow_validates_enables_without_mutation():
    flow = {
        "operations": [
            {
                "id": "grant_access",
                "verb": "grant",
                "enables": [{"actor": "member", "operations": ["op_a"]}],
            }
        ]
    }
    with warnings_module.catch_warnings():
        warnings_module.simplefilter("error")
        normalized = normalize_operation_flow(flow)
    # The raw declaration is preserved verbatim for downstream consumers.
    assert normalized["operations"][0]["enables"] == [
        {"actor": "member", "operations": ["op_a"]}
    ]


def test_normalize_operation_flow_warns_on_malformed_enables():
    flow = {"operations": [{"id": "grant_access", "enables": "broken"}]}
    with pytest.warns(UserWarning):
        normalize_operation_flow(flow)
