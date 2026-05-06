from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import yaml

import codd.cli as cli
from codd.cli import main
from codd.repair import verify_runner as verify_runner_module
from codd.repair.verify_runner import VerificationResult, run_standalone_verify


def _write_project(tmp_path: Path, *, repair: dict | None = None) -> Path:
    project = tmp_path / "project"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    config: dict = {"project": {"name": "demo", "type": "generic"}}
    if repair is not None:
        config["repair"] = repair
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return project


def _failure() -> object:
    return cli._verification_failure_report("verify", ["docs/spec.md"], ["failed"], {})


def _outcome(project: Path, status: str) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        history_session_dir=project / ".codd" / "repair_history" / "2026-05-06T00-00-00Z",
    )


def test_auto_repair_uses_standalone_verify_when_pro_missing(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path)
    monkeypatch.setattr(cli, "get_command_handler", lambda name: None)
    monkeypatch.setattr("codd.repair.verify_runner.run_standalone_verify", lambda project_root: VerificationResult(True))
    monkeypatch.setattr(cli, "_run_pro_command", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pro called")))

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 0
    assert "install codd-pro" not in result.output.lower()


def test_auto_repair_standalone_failure_launches_repair_loop(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path, repair={"approval_mode": "required"})
    captured = {}
    monkeypatch.setattr(cli, "get_command_handler", lambda name: None)
    monkeypatch.setattr(
        "codd.repair.verify_runner.run_standalone_verify",
        lambda project_root: VerificationResult(False, failure=_failure()),
    )

    def run_loop(project_root, failure, **kwargs):
        captured["project_root"] = project_root
        captured["failure"] = failure
        captured["verify_callable"] = kwargs["verify_callable"]
        return _outcome(project, "REPAIR_SUCCESS")

    monkeypatch.setattr(cli, "_run_repair_loop", run_loop)

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 0
    assert captured["project_root"] == project
    assert captured["failure"].check_name == "verify"
    assert callable(captured["verify_callable"])


def test_auto_repair_standalone_failure_requires_repair_config(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path)
    monkeypatch.setattr(cli, "get_command_handler", lambda name: None)
    monkeypatch.setattr(
        "codd.repair.verify_runner.run_standalone_verify",
        lambda project_root: VerificationResult(False, failure=_failure()),
    )

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 1
    assert "WARN: codd.yaml [repair] section is required" in result.output


def test_auto_repair_prefers_standalone_when_pro_handler_exists(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "get_command_handler", lambda name: object())
    monkeypatch.setattr(
        "codd.repair.verify_runner.run_standalone_verify",
        lambda project_root: calls.append(project_root) or VerificationResult(True),
    )

    def run_pro(name, **kwargs):
        raise AssertionError("pro called")

    monkeypatch.setattr(cli, "_run_pro_command", run_pro)

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 0
    assert calls == [project.resolve()]


def test_verify_without_auto_repair_keeps_pro_verify_when_handler_exists(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path)
    captured = {}
    monkeypatch.setattr("codd.repair.verify_runner.run_standalone_verify", lambda project_root: (_ for _ in ()).throw(AssertionError("standalone called")))

    def run_pro(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs

    monkeypatch.setattr(cli, "_run_pro_command", run_pro)

    result = CliRunner().invoke(main, ["verify", "--path", str(project)])

    assert result.exit_code == 0
    assert captured == {"name": "verify", "kwargs": {"path": str(project), "sprint": None}}


def test_standalone_verify_reports_missing_codd_yaml(tmp_path: Path):
    result = run_standalone_verify(tmp_path)

    assert result.passed is False
    assert result.failures[0].check_name == "codd_config"


def test_standalone_verify_loads_project_config(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path, repair={"approval_mode": "required"})
    captured = {}

    def run(self):
        captured["config"] = self.codd_yaml
        return VerificationResult(True)

    monkeypatch.setattr(verify_runner_module.VerifyRunner, "run", run)

    result = run_standalone_verify(project)

    assert result.passed is True
    assert captured["config"]["project"]["name"] == "demo"
    assert captured["config"]["repair"]["approval_mode"] == "required"


def test_cli_result_from_standalone_verify_maps_exit_code():
    failure = _failure()

    result = cli._cli_result_from_standalone_verify(VerificationResult(False, failure=failure))

    assert result.passed is False
    assert result.exit_code == 1
    assert result.failure is failure


def test_repair_attempt_verify_callable_uses_standalone_fallback(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path, repair={"approval_mode": "required"})
    outcomes = [VerificationResult(False, failure=_failure()), VerificationResult(True)]
    monkeypatch.setattr(cli, "get_command_handler", lambda name: None)
    monkeypatch.setattr("codd.repair.verify_runner.run_standalone_verify", lambda project_root: outcomes.pop(0))

    def run_loop(project_root, failure, **kwargs):
        retry = kwargs["verify_callable"]()
        assert retry.passed is True
        return _outcome(project, "REPAIR_SUCCESS")

    monkeypatch.setattr(cli, "_run_repair_loop", run_loop)

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 0
    assert outcomes == []
