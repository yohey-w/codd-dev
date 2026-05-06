from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codd.cli import CoddCLIError, main
from codd.coherence_engine import EventBus, use_coherence_bus
from codd.dag import DAG, Edge, Node
from codd.dag.checks.deployment_completeness import DeploymentChainViolation
from codd.deployer import (
    DeployGateResult,
    _collect_dag_completeness_gate,
    _collect_deployment_completeness_gate,
    _format_deploy_incomplete_ntfy,
    _publish_deployment_completeness_events,
    _run_deploy_gates,
    run_deploy,
)
from codd.deployment import (
    EDGE_EXECUTES_IN_ORDER,
    EDGE_PRODUCES_STATE,
    EDGE_REQUIRES_DEPLOYMENT_STEP,
    EDGE_VERIFIED_BY,
)
from codd.deploy_targets import register_target
from codd.deploy_targets.base import DeployTarget


@register_target("deployment_gate_dummy")
class DeploymentGateDummyTarget(DeployTarget):
    instances: list["DeploymentGateDummyTarget"] = []

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.calls: list[str] = []
        DeploymentGateDummyTarget.instances.append(self)

    def snapshot(self) -> dict:
        self.calls.append("snapshot")
        return {"revision": "before"}

    def dry_run(self) -> list[str]:
        self.calls.append("dry_run")
        return ["would deploy"]

    def deploy(self) -> bool:
        self.calls.append("deploy")
        return True

    def healthcheck(self) -> bool:
        self.calls.append("healthcheck")
        return True

    def rollback(self, snapshot: dict) -> bool:
        self.calls.append("rollback")
        return True


def _reset_target() -> None:
    DeploymentGateDummyTarget.instances.clear()


def _violation(broken_at: str = "missing_impl_for_step") -> DeploymentChainViolation:
    return DeploymentChainViolation(
        design_doc="docs/design/api.md",
        chain_status="INCOMPLETE",
        broken_at=broken_at,
        expected_chain=[
            "docs/design/api.md -> DEPLOYMENT.md [ok]",
            "DEPLOYMENT.md -> seed step [ok]",
            "seed step -> prisma/seed.ts [missing]",
        ],
        remediation="Add prisma/seed.ts and ensure the deploy artifact includes it.",
    )


def _complete_seed_dag(*, deploy_flow: bool = True) -> DAG:
    dag = DAG()
    dag.add_node(Node(id="docs/design/api.md", kind="design_doc", path="docs/design/api.md"))
    dag.add_node(
        Node(
            id="DEPLOYMENT.md",
            kind="deployment_doc",
            path="DEPLOYMENT.md",
            attributes={"sections": ["seed"], "post_deploy": ["npm run test:smoke"] if deploy_flow else []},
        )
    )
    dag.add_node(Node(id="prisma/seed.ts", kind="impl_file", path="prisma/seed.ts"))
    dag.add_node(
        Node(
            id="runtime:db_seed:seed_data",
            kind="runtime_state",
            attributes={"kind": "db_seed", "target": "seed_data"},
        )
    )
    dag.add_node(
        Node(
            id="verification:smoke:tests/smoke/login.test.ts",
            kind="verification_test",
            path="tests/smoke/login.test.ts",
            attributes={"kind": "smoke", "target": "login", "verification_template_ref": "playwright"},
        )
    )
    dag.add_edge(
        Edge(
            from_id="docs/design/api.md",
            to_id="DEPLOYMENT.md",
            kind=EDGE_REQUIRES_DEPLOYMENT_STEP,
            attributes={"keywords": ["seed"]},
        )
    )
    dag.add_edge(
        Edge(
            from_id="DEPLOYMENT.md",
            to_id="prisma/seed.ts",
            kind=EDGE_EXECUTES_IN_ORDER,
            attributes={"order": 1, "section": "seed"},
        )
    )
    dag.add_edge(Edge(from_id="prisma/seed.ts", to_id="runtime:db_seed:seed_data", kind=EDGE_PRODUCES_STATE))
    dag.add_edge(
        Edge(
            from_id="runtime:db_seed:seed_data",
            to_id="verification:smoke:tests/smoke/login.test.ts",
            kind=EDGE_VERIFIED_BY,
        )
    )
    return dag


def _write_codd_yaml(project: Path, extra: dict | None = None) -> None:
    codd_dir = project / "codd"
    codd_dir.mkdir()
    config = {
        "version": "0.1.0",
        "scan": {"source_dirs": [], "test_dirs": [], "doc_dirs": [], "config_files": []},
        "graph": {"store": "jsonl", "path": "codd/scan"},
        "warn_on_skip": False,
    }
    if extra:
        config.update(extra)
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _write_deploy_yaml(project: Path) -> Path:
    config = {
        "default_target": "vps",
        "targets": {"vps": {"type": "deployment_gate_dummy"}},
        "global": {"log_dir": "deploy_logs"},
    }
    path = project / "deploy.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _disable_other_gates(monkeypatch) -> None:
    monkeypatch.setattr("codd.deployer._collect_validate_gate", lambda *args: None)
    monkeypatch.setattr("codd.deployer._collect_drift_gate", lambda *args: None)
    monkeypatch.setattr("codd.deployer._collect_coverage_gate", lambda *args: None)
    monkeypatch.setattr("codd.deployer._collect_dag_completeness_gate", lambda *args: None)


def test_collect_deployment_completeness_gate_returns_violations(tmp_path):
    dag = _complete_seed_dag(deploy_flow=False)

    violations = _collect_deployment_completeness_gate(dag, tmp_path, {})

    assert len(violations) == 1
    assert violations[0].broken_at == "verification_test_not_in_deploy_flow"


def test_collect_deployment_completeness_gate_no_deployment_doc_is_backward_compatible(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="docs/design/api.md", kind="design_doc"))

    violations = _collect_deployment_completeness_gate(dag, tmp_path, {})

    assert violations == []


def test_publish_deployment_completeness_events_publishes_drift_event():
    bus = EventBus()

    with use_coherence_bus(bus):
        _publish_deployment_completeness_events([_violation()])

    assert len(bus.published_events()) == 1


def test_publish_deployment_completeness_events_sets_kind_and_red_severity():
    bus = EventBus()

    with use_coherence_bus(bus):
        _publish_deployment_completeness_events([_violation()])

    event = bus.published_events()[0]
    assert event.kind == "deployment_completeness"
    assert event.severity == "red"


def test_run_deploy_gates_blocks_on_c6_violation(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path)
    _disable_other_gates(monkeypatch)
    monkeypatch.setattr("codd.deployer._collect_deployment_completeness_gate", lambda *args: [_violation()])
    monkeypatch.setattr("codd.deployer._ntfy_deploy_incomplete", lambda *args: True)

    result = _run_deploy_gates(tmp_path)

    assert result.passed is False
    assert result.failures[0].gate == "deployment_completeness"


def test_run_deploy_gates_records_incomplete_chain_report(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path)
    _disable_other_gates(monkeypatch)
    monkeypatch.setattr("codd.deployer._collect_deployment_completeness_gate", lambda *args: [_violation()])
    monkeypatch.setattr("codd.deployer._ntfy_deploy_incomplete", lambda *args: True)

    result = _run_deploy_gates(tmp_path)

    payload = result.as_log_payload()
    assert payload["incomplete_chain_report"][0]["broken_at"] == "missing_impl_for_step"
    assert json.loads(payload["failures"][0]["details"][0])["incomplete_chain_report"]


def test_run_deploy_gates_calls_ntfy_critical_on_c6_violation(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path)
    _disable_other_gates(monkeypatch)
    calls: list[list[DeploymentChainViolation]] = []
    monkeypatch.setattr("codd.deployer._collect_deployment_completeness_gate", lambda *args: [_violation()])
    monkeypatch.setattr("codd.deployer._ntfy_deploy_incomplete", lambda violations, settings: calls.append(violations))

    _run_deploy_gates(tmp_path)

    assert calls and calls[0][0].broken_at == "missing_impl_for_step"


def test_run_deploy_gates_c6_pass_does_not_block(tmp_path, monkeypatch):
    _write_codd_yaml(tmp_path)
    _disable_other_gates(monkeypatch)
    monkeypatch.setattr("codd.deployer._collect_deployment_completeness_gate", lambda *args: [])

    result = _run_deploy_gates(tmp_path)

    assert result.passed is True


def test_run_deploy_writes_incomplete_chain_report_to_deploy_log(tmp_path, monkeypatch):
    _reset_target()
    config_path = _write_deploy_yaml(tmp_path)
    _write_codd_yaml(tmp_path)
    _disable_other_gates(monkeypatch)
    monkeypatch.setattr("codd.deployer._collect_deployment_completeness_gate", lambda *args: [_violation()])
    monkeypatch.setattr("codd.deployer._ntfy_deploy_incomplete", lambda *args: True)

    with pytest.raises(CoddCLIError, match="deployment_completeness"):
        run_deploy(tmp_path, config_path=config_path, dry_run=False)

    logs = list((tmp_path / "deploy_logs").glob("*_vps.yaml"))
    payload = yaml.safe_load(logs[0].read_text(encoding="utf-8"))
    assert payload["status"] == "gate_failed"
    assert payload["gates"]["incomplete_chain_report"][0]["design_doc"] == "docs/design/api.md"


def test_c1_to_c5_deploy_gate_excludes_deployment_completeness(tmp_path, monkeypatch):
    captured: dict[str, list[str] | tuple[str, ...] | None] = {}

    def fake_run_all_checks(project_root, settings=None, check_names=None):
        captured["check_names"] = check_names
        return []

    monkeypatch.setattr("codd.dag.runner.run_all_checks", fake_run_all_checks)

    _collect_dag_completeness_gate(tmp_path, {}, DeployGateResult())

    assert "node_completeness" in captured["check_names"]
    assert "deployment_completeness" not in captured["check_names"]


def test_deploy_gate_includes_environment_coverage(tmp_path, monkeypatch):
    captured: dict[str, list[str] | tuple[str, ...] | None] = {}

    def fake_run_all_checks(project_root, settings=None, check_names=None):
        captured["check_names"] = check_names
        return []

    monkeypatch.setattr("codd.dag.runner.run_all_checks", fake_run_all_checks)

    _collect_dag_completeness_gate(tmp_path, {}, DeployGateResult())

    assert "environment_coverage" in captured["check_names"]


def test_cli_deploy_apply_target_vps_runs_c6_gate(tmp_path, monkeypatch):
    _reset_target()
    called: list[Path] = []

    with CliRunner().isolated_filesystem(temp_dir=tmp_path):
        project = Path(".")
        _write_deploy_yaml(project)
        _write_codd_yaml(project)
        _disable_other_gates(monkeypatch)
        monkeypatch.setattr(
            "codd.deployer._collect_deployment_completeness_gate",
            lambda dag, project_root, settings: called.append(Path(project_root)) or [],
        )

        result = CliRunner().invoke(main, ["deploy", "--apply", "--target", "vps"])

    assert result.exit_code == 0
    assert called


def test_ntfy_critical_message_mentions_design_doc_broken_at_and_remediation():
    message = _format_deploy_incomplete_ntfy(_violation("state_not_produced"))

    assert "CRITICAL deploy INCOMPLETE: docs/design/api.md -> state_not_produced" in message
    assert "Add prisma/seed.ts" in message
