from __future__ import annotations

from pathlib import Path
import subprocess

from codd.deploy_targets import get_target, list_registered_target_types
from codd.deploy_targets.docker_compose import DockerComposeTarget


def _config() -> dict:
    return {
        "type": "docker_compose",
        "host": "example.com",
        "ssh_user": "deploy",
        "ssh_key": "~/.ssh/example",
        "working_dir": "/srv/app",
        "compose_file": "docker-compose.production.yml",
        "git_branch": "main",
        "git_remote": "origin",
    }


def _completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["ssh"], 0, stdout=stdout, stderr="")


def test_docker_compose_target_init():
    target = DockerComposeTarget(_config())

    assert target.host == "example.com"
    assert target.ssh_user == "deploy"
    assert target.ssh_key == Path("~/.ssh/example").expanduser()
    assert target.working_dir == "/srv/app"
    assert target.compose_file == "docker-compose.production.yml"


def test_dry_run_no_ssh(monkeypatch):
    def fail_run(*args, **kwargs):
        raise AssertionError("dry_run must not open SSH")

    monkeypatch.setattr("codd.deploy_targets.docker_compose.subprocess.run", fail_run)
    actions = DockerComposeTarget(_config()).dry_run()

    assert any("deploy@example.com" in action for action in actions)
    assert any("docker compose -f docker-compose.production.yml up -d" in action for action in actions)


def test_snapshot_calls_ssh(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return _completed("abc123\n")

    monkeypatch.setattr("codd.deploy_targets.docker_compose.subprocess.run", fake_run)

    DockerComposeTarget(_config()).snapshot()

    assert calls[0][0] == "ssh"
    assert calls[0][2] == str(Path("~/.ssh/example").expanduser())
    assert "cd /srv/app && git rev-parse HEAD" in calls[0][-1]


def test_snapshot_returns_commit(monkeypatch):
    monkeypatch.setattr(
        "codd.deploy_targets.docker_compose.subprocess.run",
        lambda *args, **kwargs: _completed("abc123\n"),
    )

    snapshot = DockerComposeTarget(_config()).snapshot()

    assert snapshot == {
        "git_commit": "abc123",
        "host": "example.com",
        "working_dir": "/srv/app",
    }


def test_deploy_calls_git_pull(monkeypatch):
    target = DockerComposeTarget(_config())
    commands: list[str] = []
    monkeypatch.setattr(target, "_run_ssh", lambda command, check=True: commands.append(command) or _completed())

    target.deploy()

    assert "git pull origin main" in commands[0]


def test_deploy_calls_docker_compose(monkeypatch):
    target = DockerComposeTarget(_config())
    commands: list[str] = []
    monkeypatch.setattr(target, "_run_ssh", lambda command, check=True: commands.append(command) or _completed())

    target.deploy()

    assert "docker compose -f docker-compose.production.yml pull" in commands[1]
    assert "docker compose -f docker-compose.production.yml up -d" in commands[1]


def test_deploy_returns_true_on_success(monkeypatch):
    target = DockerComposeTarget(_config())
    monkeypatch.setattr(target, "_run_ssh", lambda command, check=True: _completed())

    assert target.deploy() is True


def test_deploy_returns_false_on_ssh_error(monkeypatch, capsys):
    target = DockerComposeTarget(_config())

    def fail_ssh(command, check=True):
        raise subprocess.CalledProcessError(255, ["ssh"], stderr="boom")

    monkeypatch.setattr(target, "_run_ssh", fail_ssh)

    assert target.deploy() is False
    assert "Deploy failed: boom" in capsys.readouterr().out


def test_rollback_calls_git_checkout(monkeypatch):
    target = DockerComposeTarget(_config())
    commands: list[str] = []
    monkeypatch.setattr(target, "_run_ssh", lambda command, check=True: commands.append(command) or _completed())

    assert target.rollback({"git_commit": "abc123"}) is True
    assert "git checkout abc123" in commands[0]
    assert "docker compose -f docker-compose.production.yml up -d" in commands[0]


def test_rollback_no_commit_in_snapshot(capsys):
    assert DockerComposeTarget(_config()).rollback({}) is False
    assert "No commit hash in snapshot" in capsys.readouterr().out


def test_register_target_docker_compose():
    assert get_target("docker_compose") is DockerComposeTarget
    assert "docker_compose" in list_registered_target_types()


def test_healthcheck_delegates_to_deployer(monkeypatch):
    config = _config()
    config["healthcheck"] = {
        "url": "http://example.test/health",
        "expected_status": 204,
        "timeout_seconds": 12,
        "retries": 4,
    }
    calls = []

    def fake_healthcheck(*, url, expected_status, timeout_seconds, retries):
        calls.append((url, expected_status, timeout_seconds, retries))
        return True

    monkeypatch.setattr("codd.deployer.run_healthcheck", fake_healthcheck)

    assert DockerComposeTarget(config).healthcheck() is True
    assert calls == [("http://example.test/health", 204, 12, 4)]
