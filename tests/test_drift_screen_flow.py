"""Tests for screen-flow design vs implementation drift detection."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import yaml

import codd.drift as drift_module
from codd.cli import main
from codd.coherence_engine import EventBus
from codd.drift import ScreenFlowDriftResult, compute_screen_flow_drift
from codd.screen_transition_extractor import ScreenTransition


def _write_codd_yaml(project: Path) -> None:
    codd_dir = project / ".codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    (codd_dir / "codd.yaml").write_text("filesystem_routes: []\n", encoding="utf-8")


def _write_design_edges(project: Path, edges: list[dict]) -> Path:
    output_path = project / "docs" / "extracted" / "screen-transitions.yaml"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump({"edges": edges}, sort_keys=False), encoding="utf-8")
    return output_path


def _write_e2e_spec(project: Path, route: str) -> None:
    spec_path = project / "tests" / "e2e" / "navigation.spec.ts"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(f"await expect(page).toHaveURL('{route}')\n", encoding="utf-8")


def _transition(
    from_route: str,
    to_route: str,
    trigger: str,
    source_file: str = "app/login/page.tsx",
) -> ScreenTransition:
    return ScreenTransition(
        from_route=from_route,
        to_route=to_route,
        trigger=trigger,
        kind="link",
        source_file=source_file,
        source_line=3,
    )


def test_compute_screen_flow_drift_no_diff(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [{"from": "/login", "to": "/dashboard", "trigger": "Link[href]"}])
    monkeypatch.setattr(
        "codd.screen_transition_extractor.extract_transitions",
        lambda project_root, src_dirs=None: [_transition("/login", "/dashboard", "Link[href]")],
    )

    result = compute_screen_flow_drift(tmp_path)

    assert result.design_only == []
    assert result.impl_only == []
    assert result.mismatch == []
    assert result.total_design == 1
    assert result.total_impl == 1


def test_compute_screen_flow_drift_design_only(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [{"from": "/login", "to": "/dashboard", "trigger": "submit_credentials"}])
    monkeypatch.setattr("codd.screen_transition_extractor.extract_transitions", lambda project_root, src_dirs=None: [])

    result = compute_screen_flow_drift(tmp_path)

    assert [(edge["from"], edge["to"], edge["trigger"]) for edge in result.design_only] == [
        ("/login", "/dashboard", "submit_credentials")
    ]
    assert result.impl_only == []


def test_compute_screen_flow_drift_impl_only(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [])
    monkeypatch.setattr(
        "codd.screen_transition_extractor.extract_transitions",
        lambda project_root, src_dirs=None: [_transition("/admin/users", "/admin/users/new", "router.push()")],
    )

    result = compute_screen_flow_drift(tmp_path)

    assert result.design_only == []
    assert [(edge["from"], edge["to"], edge["source_file"]) for edge in result.impl_only] == [
        ("/admin/users", "/admin/users/new", "app/login/page.tsx")
    ]


def test_compute_screen_flow_drift_mismatch(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [{"from": "/courses", "to": "/courses/:id", "trigger": "link_click"}])
    monkeypatch.setattr(
        "codd.screen_transition_extractor.extract_transitions",
        lambda project_root, src_dirs=None: [_transition("/courses", "/courses/:id", "router.push()")],
    )

    result = compute_screen_flow_drift(tmp_path)

    assert result.design_only == []
    assert result.impl_only == []
    assert result.mismatch == [
        {
            "edge": {"from": "/courses", "to": "/courses/:id"},
            "design_trigger": "link_click",
            "impl_trigger": "router.push()",
        }
    ]


def test_screen_flow_drift_result_dataclass_attributes():
    result = ScreenFlowDriftResult(
        design_only=[{"from": "/old", "to": "/new", "trigger": "click"}],
        impl_only=[],
        mismatch=[],
        total_design=1,
        total_impl=0,
    )

    assert result.design_only[0]["from"] == "/old"
    assert result.total_design == 1
    assert result.total_impl == 0


def test_compute_screen_flow_drift_publishes_coherence_events(tmp_path, monkeypatch):
    _write_design_edges(tmp_path, [{"from": "/login", "to": "/dashboard", "trigger": "submit"}])
    monkeypatch.setattr("codd.screen_transition_extractor.extract_transitions", lambda project_root, src_dirs=None: [])
    bus = EventBus()
    monkeypatch.setattr(drift_module, "_coherence_bus", bus)

    result = compute_screen_flow_drift(tmp_path)

    assert len(result.design_only) == 1
    events = bus.published_events()
    assert [event.kind for event in events] == ["screen_flow_design_drift"]
    assert events[0].source_artifact == "screen_transitions"
    assert events[0].target_artifact == "implementation"
    assert events[0].severity == "amber"


def test_compute_screen_flow_drift_missing_yaml_is_empty(tmp_path, monkeypatch):
    def fail_extract(project_root, src_dirs=None):
        raise AssertionError("extract_transitions should not run when design YAML is missing")

    monkeypatch.setattr("codd.screen_transition_extractor.extract_transitions", fail_extract)

    result = compute_screen_flow_drift(tmp_path)

    assert result == ScreenFlowDriftResult(
        design_only=[],
        impl_only=[],
        mismatch=[],
        total_design=0,
        total_impl=0,
    )


def test_cli_drift_screen_flow_design_only_exit_one(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path)
    _write_design_edges(tmp_path, [{"from": "/login", "to": "/dashboard", "trigger": "submit_credentials"}])
    monkeypatch.setattr("codd.screen_transition_extractor.extract_transitions", lambda project_root, src_dirs=None: [])

    result = CliRunner().invoke(main, ["drift", "--screen-flow", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "=== Screen Flow Drift (Design vs Implementation) ===" in result.output
    assert "Summary: 1 design-only, 0 impl-only, 0 mismatch" in result.output


def test_cli_drift_screen_flow_and_e2e_can_run_together(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path)
    _write_design_edges(tmp_path, [{"from": "/login", "to": "/dashboard", "trigger": "Link[href]"}])
    _write_e2e_spec(tmp_path, "/dashboard")
    monkeypatch.setattr(
        "codd.screen_transition_extractor.extract_transitions",
        lambda project_root, src_dirs=None: [_transition("/login", "/dashboard", "Link[href]")],
    )

    result = CliRunner().invoke(main, ["drift", "--screen-flow", "--e2e", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "No E2E drift detected." in result.output
    assert "=== Screen Flow Drift (Design vs Implementation) ===" in result.output
    assert "Summary: 0 design-only, 0 impl-only, 0 mismatch" in result.output
