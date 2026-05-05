from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codd.cli import CoddCLIError, main
from codd.deployer import DeployGateResult, load_deploy_config, run_deploy, run_healthcheck
from codd.deploy_targets import get_target, list_registered_target_types, register_target
from codd.deploy_targets.base import DeployTarget


def _deploy_config(target_type: str = "dummy_deploy") -> dict:
    return {
        "default_target": "vps",
        "targets": {
            "vps": {
                "type": target_type,
                "host": "example.com",
                "ssh_user": "deploy",
                "ssh_key": "~/.ssh/example",
                "working_dir": "/srv/app",
            }
        },
        "global": {
            "rollback_on_healthcheck_fail": True,
            "log_dir": "deploy_logs",
        },
    }


def _write_deploy_yaml(project: Path, config: dict | None = None) -> Path:
    path = project / "deploy.yaml"
    path.write_text(yaml.safe_dump(config or _deploy_config(), sort_keys=False), encoding="utf-8")
    return path


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@register_target("dummy_deploy")
class DummyDeployTarget(DeployTarget):
    instances: list["DummyDeployTarget"] = []

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.calls: list[str] = []
        DummyDeployTarget.instances.append(self)

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


def _reset_dummy() -> None:
    DummyDeployTarget.instances.clear()


class _ScreenFlowGateStub:
    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.details = ["screen_flow_design_drift: 1"]


def _allow_deploy_gates(monkeypatch) -> None:
    monkeypatch.setattr("codd.deployer._run_deploy_gates", lambda project_root: DeployGateResult())
    monkeypatch.setattr("codd.deployer._run_screen_flow_apply_gate", lambda project_root: DeployGateResult())


def test_load_deploy_config_valid(tmp_path):
    config_path = _write_deploy_yaml(tmp_path)

    config = load_deploy_config(config_path)

    assert config["default_target"] == "vps"
    assert config["targets"]["vps"]["ssh_key"] == "~/.ssh/example"


def test_load_deploy_config_missing(tmp_path):
    with pytest.raises(CoddCLIError, match="Deploy config not found"):
        load_deploy_config(tmp_path / "missing.yaml")


def test_load_deploy_config_ssh_key_content_rejected(tmp_path):
    config = _deploy_config()
    config["targets"]["vps"]["ssh_key"] = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret"
    config_path = _write_deploy_yaml(tmp_path, config)

    with pytest.raises(CoddCLIError, match="path reference"):
        load_deploy_config(config_path)


def test_register_and_get_target():
    @register_target("registry_test_target")
    class RegistryTarget(DummyDeployTarget):
        pass

    assert get_target("registry_test_target") is RegistryTarget


def test_get_target_unknown_raises():
    with pytest.raises(CoddCLIError, match="Unknown deploy target type"):
        get_target("unknown_deploy_target")


def test_list_registered_target_types():
    @register_target("registry_list_target")
    class RegistryListTarget(DummyDeployTarget):
        pass

    assert "registry_list_target" in list_registered_target_types()


def test_run_healthcheck_success(monkeypatch):
    monkeypatch.setattr("codd.deployer.urlopen", lambda request, timeout: _FakeResponse(200))

    assert run_healthcheck("http://example.test/health", 200, 1, 1) is True


def test_run_healthcheck_failure(monkeypatch):
    monkeypatch.setattr("codd.deployer.urlopen", lambda request, timeout: _FakeResponse(503))

    assert run_healthcheck("http://example.test/health", 200, 1, 1) is False


def test_run_healthcheck_retries(monkeypatch):
    attempts = iter([OSError("network"), _FakeResponse(200)])

    def fake_urlopen(request, timeout):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("codd.deployer.urlopen", fake_urlopen)
    monkeypatch.setattr("codd.deployer.time.sleep", lambda seconds: None)

    assert run_healthcheck("http://example.test/health", 200, 1, 2) is True


def test_deploy_dry_run(tmp_path):
    _reset_dummy()
    config_path = _write_deploy_yaml(tmp_path)

    exit_code = run_deploy(tmp_path, config_path=config_path, dry_run=True)

    assert exit_code == 0
    assert DummyDeployTarget.instances[-1].calls == ["dry_run"]


def test_deploy_dry_run_skips_screen_flow_gate(tmp_path, monkeypatch):
    _reset_dummy()
    config_path = _write_deploy_yaml(tmp_path)

    def fail_gate(project_root):
        raise AssertionError("dry-run must not run screen-flow gate")

    monkeypatch.setattr("codd.deployer._run_screen_flow_apply_gate", fail_gate)

    exit_code = run_deploy(tmp_path, config_path=config_path, dry_run=True)

    assert exit_code == 0
    assert DummyDeployTarget.instances[-1].calls == ["dry_run"]


def test_deploy_apply_calls_target(tmp_path, monkeypatch):
    _reset_dummy()
    _allow_deploy_gates(monkeypatch)
    config_path = _write_deploy_yaml(tmp_path)

    exit_code = run_deploy(tmp_path, config_path=config_path, dry_run=False)

    assert exit_code == 0
    assert DummyDeployTarget.instances[-1].calls == ["snapshot", "deploy", "healthcheck"]


def test_deploy_apply_blocks_on_screen_flow_gate_failure(tmp_path, monkeypatch):
    _reset_dummy()
    _allow_deploy_gates(monkeypatch)
    config_path = _write_deploy_yaml(tmp_path)
    monkeypatch.setattr(
        "codd.deployer._run_screen_flow_apply_gate",
        lambda project_root: _ScreenFlowGateStub(False),
    )

    exit_code = run_deploy(tmp_path, config_path=config_path, dry_run=False)

    assert exit_code == 1
    assert DummyDeployTarget.instances[-1].calls == []


def test_deploy_healthcheck_fail_triggers_rollback(tmp_path, monkeypatch):
    _reset_dummy()
    _allow_deploy_gates(monkeypatch)
    config = _deploy_config()
    config["targets"]["vps"]["healthcheck"] = {
        "url": "http://example.test/health",
        "expected_status": 200,
        "timeout_seconds": 60,
        "retries": 3,
    }
    config_path = _write_deploy_yaml(tmp_path, config)
    monkeypatch.setattr("codd.deployer.run_healthcheck", lambda *args, **kwargs: False)

    exit_code = run_deploy(tmp_path, config_path=config_path, dry_run=False)

    assert exit_code == 1
    assert DummyDeployTarget.instances[-1].calls == ["snapshot", "deploy", "rollback"]


def test_deploy_log_written(tmp_path, monkeypatch):
    _reset_dummy()
    _allow_deploy_gates(monkeypatch)
    config_path = _write_deploy_yaml(tmp_path)

    exit_code = run_deploy(tmp_path, config_path=config_path, dry_run=False)

    assert exit_code == 0
    logs = list((tmp_path / "deploy_logs").glob("*_vps.yaml"))
    assert len(logs) == 1
    payload = yaml.safe_load(logs[0].read_text(encoding="utf-8"))
    assert payload["status"] == "deployed"
    assert payload["target"] == "vps"


def test_cli_deploy_help():
    result = CliRunner().invoke(main, ["deploy", "--help"])

    assert result.exit_code == 0
    assert "--apply" in result.output
    assert "default: dry-run" in result.output


def test_cli_deploy_dry_run_default(tmp_path):
    _reset_dummy()
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        _write_deploy_yaml(Path("."))
        result = runner.invoke(main, ["deploy"])

    assert result.exit_code == 0
    assert DummyDeployTarget.instances[-1].calls == ["dry_run"]
