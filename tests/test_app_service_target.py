from __future__ import annotations

import subprocess

from codd.deploy_targets import get_target, list_registered_target_types
from codd.deploy_targets.app_service import AppServiceTarget


def _config() -> dict:
    return {
        "type": "app_service",
        "subscription_id": "${env:AZURE_SUBSCRIPTION_ID}",
        "resource_group": "rg-osato-lms-prod",
        "app_name": "osato-lms-prod",
        "package_path": "release.zip",
    }


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["az"], returncode, stdout=stdout, stderr="")


def test_app_service_target_init(monkeypatch):
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-123")

    target = AppServiceTarget(_config())

    assert target.subscription_id == "sub-123"
    assert target.resource_group == "rg-osato-lms-prod"
    assert target.app_name == "osato-lms-prod"
    assert target.package_path == "release.zip"


def test_expand_env_var(monkeypatch):
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-from-env")

    assert AppServiceTarget(_config()).subscription_id == "sub-from-env"


def test_missing_env_var_keeps_placeholder(monkeypatch):
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)

    assert AppServiceTarget(_config()).subscription_id == "${env:AZURE_SUBSCRIPTION_ID}"


def test_dry_run_no_az(monkeypatch):
    def fail_run(*args, **kwargs):
        raise AssertionError("dry_run must not call az")

    monkeypatch.setattr("codd.deploy_targets.app_service.subprocess.run", fail_run)

    actions = AppServiceTarget(_config()).dry_run()

    assert any("az account show" in action for action in actions)
    assert any("az webapp show" in action for action in actions)
    assert any("az webapp deploy" in action for action in actions)


def test_snapshot_calls_az_webapp_show(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(command, capture_output, text, check):
        calls.append(command)
        return _completed('{"defaultHostName": "osato-lms-prod.azurewebsites.net"}')

    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-123")
    monkeypatch.setattr("codd.deploy_targets.app_service.subprocess.run", fake_run)

    AppServiceTarget(_config()).snapshot()

    assert calls[0][:3] == ["az", "webapp", "show"]
    assert "--resource-group" in calls[0]
    assert "rg-osato-lms-prod" in calls[0]
    assert "--name" in calls[0]
    assert "osato-lms-prod" in calls[0]
    assert calls[0][-2:] == ["--subscription", "sub-123"]


def test_snapshot_returns_info(monkeypatch):
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-123")
    monkeypatch.setattr(
        "codd.deploy_targets.app_service.subprocess.run",
        lambda *args, **kwargs: _completed(
            '{"defaultHostName": "osato-lms-prod.azurewebsites.net", "state": "Running"}'
        ),
    )

    snapshot = AppServiceTarget(_config()).snapshot()

    assert snapshot["app_name"] == "osato-lms-prod"
    assert snapshot["resource_group"] == "rg-osato-lms-prod"
    assert snapshot["default_host_name"] == "osato-lms-prod.azurewebsites.net"
    assert snapshot["state"] == "Running"


def test_snapshot_returns_basic_info_on_az_error(monkeypatch):
    monkeypatch.setattr(
        "codd.deploy_targets.app_service.subprocess.run",
        lambda *args, **kwargs: _completed(returncode=1),
    )

    snapshot = AppServiceTarget(_config()).snapshot()

    assert snapshot == {
        "app_name": "osato-lms-prod",
        "resource_group": "rg-osato-lms-prod",
    }


def test_deploy_calls_az_webapp_deploy(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(command, capture_output, text, check):
        calls.append(command)
        return _completed()

    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-123")
    monkeypatch.setattr("codd.deploy_targets.app_service.subprocess.run", fake_run)

    AppServiceTarget(_config()).deploy()

    assert calls[0][:3] == ["az", "webapp", "deploy"]
    assert "--src-path" in calls[0]
    assert "release.zip" in calls[0]
    assert calls[0][calls[0].index("--type") + 1] == "zip"
    assert calls[0][-2:] == ["--subscription", "sub-123"]


def test_deploy_returns_true_on_success(monkeypatch):
    target = AppServiceTarget(_config())
    monkeypatch.setattr(target, "_run_az", lambda *args, check=True: _completed())

    assert target.deploy() is True


def test_deploy_returns_false_on_az_error(monkeypatch, capsys):
    target = AppServiceTarget(_config())

    def fail_az(*args, check=True):
        raise subprocess.CalledProcessError(1, ["az"], stderr="boom")

    monkeypatch.setattr(target, "_run_az", fail_az)

    assert target.deploy() is False
    assert "Azure App Service deploy failed: boom" in capsys.readouterr().out


def test_rollback_calls_az_restart(monkeypatch):
    target = AppServiceTarget(_config())
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(target, "_run_az", lambda *args, check=True: calls.append(args) or _completed())

    assert target.rollback({}) is True
    assert calls == [
        (
            "webapp",
            "restart",
            "--resource-group",
            "rg-osato-lms-prod",
            "--name",
            "osato-lms-prod",
        )
    ]


def test_healthcheck_delegates_to_deployer(monkeypatch):
    config = _config()
    config["healthcheck"] = {
        "url": "https://osato-lms-prod.azurewebsites.net/api/health",
        "expected_status": 204,
        "timeout_seconds": 12,
        "retries": 4,
    }
    calls = []

    def fake_healthcheck(*, url, expected_status, timeout_seconds, retries):
        calls.append((url, expected_status, timeout_seconds, retries))
        return True

    monkeypatch.setattr("codd.deployer.run_healthcheck", fake_healthcheck)

    assert AppServiceTarget(config).healthcheck() is True
    assert calls == [("https://osato-lms-prod.azurewebsites.net/api/health", 204, 12, 4)]


def test_register_target_app_service():
    assert get_target("app_service") is AppServiceTarget
    assert "app_service" in list_registered_target_types()
