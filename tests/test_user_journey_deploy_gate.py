from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.coherence_engine import EventBus, use_coherence_bus
from codd.dag import DAG, Node
from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceResult
from codd.deployer import (
    DeployGateResult,
    _collect_dag_completeness_gate,
    _collect_user_journey_coherence_gate,
    _collect_user_journey_coherence_gate_result,
    _format_user_journey_coherence_ntfy,
    _ntfy_user_journey_coherence_fail,
    _publish_user_journey_coherence_events,
    _run_deploy_gates,
)


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _doc_with_frontmatter(frontmatter: dict, body: str = "# Auth\n") -> str:
    return yaml.safe_dump(frontmatter, explicit_start=True, sort_keys=False) + "---\n" + body


def _write_codd_yaml(project: Path) -> None:
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "0.1.0",
                "scan": {"source_dirs": [], "test_dirs": [], "doc_dirs": [], "config_files": []},
                "graph": {"store": "jsonl", "path": "codd/scan"},
                "warn_on_skip": False,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_design(project: Path, journeys: list[dict]) -> None:
    _write(project / "docs" / "design" / "auth.md", _doc_with_frontmatter({"user_journeys": journeys}))


def _journey(**overrides) -> dict:
    journey = {
        "name": "login_to_dashboard",
        "criticality": "critical",
        "steps": [{"action": "expect_url", "value": "/dashboard"}],
        "required_capabilities": ["tls_termination", "cookie_security_secure_attribute"],
        "expected_outcome_refs": [],
    }
    journey.update(overrides)
    return journey


def _violation(severity: str = "red", **overrides) -> dict:
    violation = {
        "type": "unsatisfied_runtime_capability",
        "severity": severity,
        "user_journey": "login_to_dashboard",
        "design_doc": "docs/design/auth.md",
        "required_capability": "tls_termination",
    }
    violation.update(overrides)
    return violation


def _c7_result(violations: list[dict] | None = None) -> UserJourneyCoherenceResult:
    items = violations or []
    red_count = sum(1 for item in items if item.get("severity") == "red")
    amber_count = sum(1 for item in items if item.get("severity") == "amber")
    return UserJourneyCoherenceResult(
        severity="red" if red_count else ("amber" if amber_count else "info"),
        status="fail" if red_count else "pass",
        message="test result",
        violations=items,
        journey_reports=[{"user_journey": "login_to_dashboard", "violations": items}],
        passed=red_count == 0,
    )


def _disable_other_gates(monkeypatch) -> None:
    monkeypatch.setattr("codd.deployer._collect_validate_gate", lambda *args: None)
    monkeypatch.setattr("codd.deployer._collect_drift_gate", lambda *args: None)
    monkeypatch.setattr("codd.deployer._collect_coverage_gate", lambda *args: None)
    monkeypatch.setattr("codd.deployer._collect_dag_completeness_gate", lambda *args: None)


def test_collect_user_journey_coherence_gate_passes_without_violations(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="docs/design/auth.md", kind="design_doc", attributes={}))

    result = _collect_user_journey_coherence_gate(dag, tmp_path, {})

    assert result.passed is True
    assert result.violations == []


def test_run_deploy_gates_blocks_on_c7_red_violation(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path)
    _disable_other_gates(monkeypatch)
    monkeypatch.setattr("codd.deployer._collect_deployment_completeness_gate", lambda *args: [])
    monkeypatch.setattr("codd.deployer._collect_user_journey_coherence_gate", lambda *args: _c7_result([_violation()]))
    monkeypatch.setattr("codd.deployer._ntfy_user_journey_coherence_fail", lambda *args: True)

    result = _run_deploy_gates(tmp_path)

    assert result.passed is False
    assert result.failures[0].gate == "user_journey_coherence"


def test_run_deploy_gates_allows_c7_amber_warning(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path)
    _disable_other_gates(monkeypatch)
    monkeypatch.setattr("codd.deployer._collect_deployment_completeness_gate", lambda *args: [])
    monkeypatch.setattr(
        "codd.deployer._collect_user_journey_coherence_gate",
        lambda *args: _c7_result([_violation("amber", type="journey_step_no_assertion")]),
    )

    result = _run_deploy_gates(tmp_path)

    assert result.passed is True
    assert result.warnings == ["user_journey_coherence: 1 amber violation(s)"]


def test_run_deploy_gates_executes_c7_after_c6(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path)
    order: list[str] = []
    monkeypatch.setattr("codd.deployer._collect_validate_gate", lambda *args: order.append("validate"))
    monkeypatch.setattr("codd.deployer._collect_drift_gate", lambda *args: order.append("drift"))
    monkeypatch.setattr("codd.deployer._collect_coverage_gate", lambda *args: order.append("coverage"))
    monkeypatch.setattr("codd.deployer._collect_dag_completeness_gate", lambda *args: order.append("dag"))
    monkeypatch.setattr(
        "codd.deployer._collect_deployment_completeness_gate_result",
        lambda *args: order.append("deployment_completeness"),
    )
    monkeypatch.setattr(
        "codd.deployer._collect_user_journey_coherence_gate_result",
        lambda *args: order.append("user_journey_coherence"),
    )

    _run_deploy_gates(tmp_path)

    assert order == [
        "validate",
        "drift",
        "coverage",
        "dag",
        "deployment_completeness",
        "user_journey_coherence",
    ]


def test_dag_gate_includes_c9_without_c6_or_c7(monkeypatch, tmp_path):
    captured: dict[str, list[str] | tuple[str, ...] | None] = {}

    def fake_run_all_checks(project_root, settings=None, check_names=None):
        captured["check_names"] = check_names
        return []

    monkeypatch.setattr("codd.dag.runner.run_all_checks", fake_run_all_checks)

    _collect_dag_completeness_gate(tmp_path, {}, DeployGateResult())

    assert captured["check_names"] == [
        "node_completeness",
        "edge_validity",
        "depends_on_consistency",
        "task_completion",
        "transitive_closure",
        "environment_coverage",
    ]


def test_publish_user_journey_coherence_events_publishes_kind_and_severity():
    bus = EventBus()

    with use_coherence_bus(bus):
        _publish_user_journey_coherence_events([_violation()])

    event = bus.published_events()[0]
    assert event.kind == "user_journey_coherence"
    assert event.severity == "red"


def test_publish_user_journey_coherence_events_no_violation_publishes_none():
    bus = EventBus()

    with use_coherence_bus(bus):
        _publish_user_journey_coherence_events([])

    assert bus.published_events() == []


def test_red_c7_violation_sends_ntfy_critical(monkeypatch):
    calls: list[SimpleNamespace] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(request, timeout):
        calls.append(SimpleNamespace(request=request, timeout=timeout))
        return Response()

    monkeypatch.setattr("codd.deployer.urlopen", fake_urlopen)

    sent = _ntfy_user_journey_coherence_fail([_violation()], {"ntfy_topic": "topic"})

    assert sent is True
    assert calls[0].request.data == b"C7 user_journey_coherence FAIL: 1 violations"
    assert calls[0].request.get_header("Priority") == "urgent"


def test_amber_c7_violation_does_not_send_ntfy(monkeypatch):
    monkeypatch.setattr("codd.deployer.urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))

    sent = _ntfy_user_journey_coherence_fail([_violation("amber")], {"ntfy_topic": "topic"})

    assert sent is False


def test_dag_journeys_cli_lists_design_doc_and_name(tmp_path):
    _write_codd_yaml(tmp_path)
    _write_design(tmp_path, [_journey()])

    result = CliRunner().invoke(main, ["dag", "journeys", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "docs/design/auth.md" in result.output
    assert "login_to_dashboard" in result.output


def test_dag_journeys_cli_no_journeys_outputs_empty_text(tmp_path):
    _write_codd_yaml(tmp_path)
    _write(tmp_path / "docs" / "design" / "auth.md", "# Auth\n")

    result = CliRunner().invoke(main, ["dag", "journeys", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert result.output == ""


def test_dag_journeys_cli_json_outputs_structured_rows(tmp_path):
    _write_codd_yaml(tmp_path)
    _write_design(tmp_path, [_journey()])

    result = CliRunner().invoke(main, ["dag", "journeys", "--path", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["design_doc"] == "docs/design/auth.md"
    assert payload[0]["name"] == "login_to_dashboard"


def test_dag_journeys_cli_displays_criticality(tmp_path):
    _write_codd_yaml(tmp_path)
    _write_design(tmp_path, [_journey(criticality="important")])

    result = CliRunner().invoke(main, ["dag", "journeys", "--path", str(tmp_path)])

    assert "login_to_dashboard [important]" in result.output


def test_dag_journeys_cli_displays_required_capabilities(tmp_path):
    _write_codd_yaml(tmp_path)
    _write_design(tmp_path, [_journey(required_capabilities=["tls_termination", "cookie_persistence"])])

    result = CliRunner().invoke(main, ["dag", "journeys", "--path", str(tmp_path)])

    assert "requires: tls_termination, cookie_persistence" in result.output


def test_c7_gate_records_report_payload(tmp_path, monkeypatch):
    monkeypatch.setattr("codd.deployer._collect_user_journey_coherence_gate", lambda *args: _c7_result([_violation()]))
    monkeypatch.setattr("codd.deployer._ntfy_user_journey_coherence_fail", lambda *args: True)
    result = DeployGateResult()

    _collect_user_journey_coherence_gate_result(tmp_path, {}, result)

    assert result.reports["user_journey_coherence_report"][0]["user_journey"] == "login_to_dashboard"


def test_format_user_journey_coherence_ntfy_counts_violations():
    assert _format_user_journey_coherence_ntfy([_violation(), _violation()]) == (
        "C7 user_journey_coherence FAIL: 2 violations"
    )
