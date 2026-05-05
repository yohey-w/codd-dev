from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.cli import CoddCLIError
from codd.deployer import DeployGateResult, _run_deploy_gates, run_deploy
from codd.deploy_targets import register_target
from codd.deploy_targets.base import DeployTarget


def _deploy_config() -> dict:
    return {
        "default_target": "vps",
        "targets": {"vps": {"type": "deploy_gate_dummy"}},
        "global": {"log_dir": "deploy_logs"},
    }


def _write_deploy_yaml(project: Path) -> Path:
    path = project / "deploy.yaml"
    path.write_text(yaml.safe_dump(_deploy_config(), sort_keys=False), encoding="utf-8")
    return path


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


def _failure_result(gate: str, message: str) -> DeployGateResult:
    result = DeployGateResult()
    result.add_failure(gate, message)
    return result


@register_target("deploy_gate_dummy")
class DeployGateDummyTarget(DeployTarget):
    instances: list["DeployGateDummyTarget"] = []

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.calls: list[str] = []
        DeployGateDummyTarget.instances.append(self)

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
    DeployGateDummyTarget.instances.clear()


def test_deploy_gate_blocks_on_validate_fail(tmp_path, monkeypatch):
    _reset_target()
    config_path = _write_deploy_yaml(tmp_path)
    monkeypatch.setattr(
        "codd.deployer._run_deploy_gates",
        lambda project_root: _failure_result("validate", "frontmatter failed"),
    )

    with pytest.raises(CoddCLIError, match="Deploy blocked: gate failed"):
        run_deploy(tmp_path, config_path=config_path, dry_run=False)

    assert DeployGateDummyTarget.instances[-1].calls == []


def test_deploy_gate_blocks_on_drift_fail(tmp_path, monkeypatch):
    _reset_target()
    config_path = _write_deploy_yaml(tmp_path)
    monkeypatch.setattr(
        "codd.deployer._run_deploy_gates",
        lambda project_root: _failure_result("drift", "1 drift(s)"),
    )

    with pytest.raises(CoddCLIError, match="drift"):
        run_deploy(tmp_path, config_path=config_path, dry_run=False)

    assert DeployGateDummyTarget.instances[-1].calls == []


def test_deploy_gate_blocks_on_coverage_fail(tmp_path, monkeypatch):
    _reset_target()
    config_path = _write_deploy_yaml(tmp_path)
    monkeypatch.setattr(
        "codd.deployer._run_deploy_gates",
        lambda project_root: _failure_result("coverage", "e2e_coverage failed"),
    )

    with pytest.raises(CoddCLIError, match="coverage"):
        run_deploy(tmp_path, config_path=config_path, dry_run=False)

    assert DeployGateDummyTarget.instances[-1].calls == []


def test_deploy_gate_passes_all_ok(tmp_path, monkeypatch):
    _reset_target()
    config_path = _write_deploy_yaml(tmp_path)
    monkeypatch.setattr("codd.deployer._run_deploy_gates", lambda project_root: DeployGateResult())

    exit_code = run_deploy(tmp_path, config_path=config_path, dry_run=False)

    assert exit_code == 0
    assert DeployGateDummyTarget.instances[-1].calls == ["snapshot", "deploy", "healthcheck"]


def test_deploy_gate_skipped_on_dry_run(tmp_path, monkeypatch):
    _reset_target()
    config_path = _write_deploy_yaml(tmp_path)

    def fail_if_called(project_root):
        raise AssertionError("dry-run must skip deploy gates")

    monkeypatch.setattr("codd.deployer._run_deploy_gates", fail_if_called)

    exit_code = run_deploy(tmp_path, config_path=config_path, dry_run=True)

    assert exit_code == 0
    assert DeployGateDummyTarget.instances[-1].calls == ["dry_run"]


def test_gate_failure_message_format():
    result = DeployGateResult()
    result.add_failure("coverage", "2 metric(s) failed", [f"detail {index}" for index in range(7)])

    message = result.format_failures()

    assert "- coverage: 2 metric(s) failed" in message
    assert "detail 0" in message
    assert "... 2 more" in message


def test_deploy_gate_blocks_when_codd_config_missing(tmp_path):
    result = _run_deploy_gates(tmp_path)

    assert result.passed is False
    assert any(failure.message == "CoDD config dir not found" for failure in result.failures)
