from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd import drift_linkers
from codd.coherence_engine import EventBus
from codd.coverage_metrics import compute_screen_flow_coverage
from codd.drift import ScreenFlowDriftResult, compute_screen_flow_drift
from codd.drift_linkers.screen_flow import ScreenFlowGate, ScreenFlowGateResult
from codd.screen_transition_extractor import ScreenTransition


def _write_design_edges(project: Path, edges: list[dict]) -> Path:
    output_path = project / "docs" / "extracted" / "screen-transitions.yaml"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump({"edges": edges}, sort_keys=False), encoding="utf-8")
    return output_path


def _transition(from_route: str, to_route: str, trigger: str = "Link[href]") -> ScreenTransition:
    return ScreenTransition(
        from_route=from_route,
        to_route=to_route,
        trigger=trigger,
        kind="link",
        source_file="app/login/page.tsx",
        source_line=3,
    )


def test_screen_flow_gate_registered():
    assert drift_linkers.get_registry()["screen_flow"] is ScreenFlowGate


def test_no_transitions_yaml_skip_with_warn(tmp_path):
    with pytest.warns(UserWarning, match="screen-transitions.yaml"):
        result = ScreenFlowGate(project_root=tmp_path, settings={"apply": True}).run()

    assert isinstance(result, ScreenFlowGateResult)
    assert result.passed is True
    assert result.skipped is True
    assert result.status == "skipped:missing_screen_transitions"


def test_missing_transition_detected(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [{"from": "/login", "to": "/dashboard", "trigger": "submit"}])
    monkeypatch.setattr("codd.screen_transition_extractor.extract_transitions", lambda project_root, src_dirs=None: [])

    result = ScreenFlowGate(project_root=tmp_path, settings={"apply": True}).run()

    assert result.passed is False
    assert result.drift.design_only[0]["from"] == "/login"
    assert result.drift_count == 1


def test_extra_transition_detected(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [])
    monkeypatch.setattr(
        "codd.screen_transition_extractor.extract_transitions",
        lambda project_root, src_dirs=None: [_transition("/admin", "/admin/users")],
    )

    result = ScreenFlowGate(project_root=tmp_path, settings={"apply": True}).run()

    assert result.passed is False
    assert result.drift.impl_only[0]["to"] == "/admin/users"
    assert result.drift_count == 1


def test_exact_match_no_drift(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [{"from": "/login", "to": "/dashboard", "trigger": "Link[href]"}])
    monkeypatch.setattr(
        "codd.screen_transition_extractor.extract_transitions",
        lambda project_root, src_dirs=None: [_transition("/login", "/dashboard")],
    )

    result = ScreenFlowGate(project_root=tmp_path, settings={"apply": True}).run()

    assert result.passed is True
    assert result.status == "passed"
    assert result.drift_count == 0


def test_drift_event_published(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [{"from": "/login", "to": "/dashboard", "trigger": "submit"}])
    monkeypatch.setattr("codd.screen_transition_extractor.extract_transitions", lambda project_root, src_dirs=None: [])
    bus = EventBus()

    result = ScreenFlowGate(
        project_root=tmp_path,
        settings={"apply": True, "coherence_bus": bus},
    ).run()

    assert result.passed is False
    assert [event.kind for event in bus.published_events()] == ["screen_flow_design_drift"]


def test_coverage_gate_connected(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [{"from": "/login", "to": "/dashboard", "trigger": "submit"}])
    monkeypatch.setattr("codd.screen_transition_extractor.extract_transitions", lambda project_root, src_dirs=None: [])

    result = compute_screen_flow_coverage(tmp_path, {}, threshold=100.0)

    assert result.passed is False
    assert result.uncovered == 1
    assert "screen_flow_design_drift: 1" in result.details


def test_regression_existing_drift_result_unchanged(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [{"from": "/courses", "to": "/courses/:id", "trigger": "link_click"}])
    monkeypatch.setattr(
        "codd.screen_transition_extractor.extract_transitions",
        lambda project_root, src_dirs=None: [_transition("/courses", "/courses/:id", "router.push()")],
    )

    result = compute_screen_flow_drift(tmp_path)

    assert isinstance(result, ScreenFlowDriftResult)
    assert result.mismatch == [
        {
            "edge": {"from": "/courses", "to": "/courses/:id"},
            "design_trigger": "link_click",
            "impl_trigger": "router.push()",
        }
    ]


def test_dry_run_skips_without_extracting(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [{"from": "/login", "to": "/dashboard", "trigger": "submit"}])

    def fail_extract(project_root, src_dirs=None):
        raise AssertionError("dry-run must not extract implementation transitions")

    monkeypatch.setattr("codd.screen_transition_extractor.extract_transitions", fail_extract)

    result = ScreenFlowGate(project_root=tmp_path, settings={"dry_run": True}).run()

    assert result.passed is True
    assert result.skipped is True
    assert result.status == "skipped:dry_run"
