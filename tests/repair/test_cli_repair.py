from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from click.testing import CliRunner
import pytest
import yaml

import codd.cli as cli
import codd.repair as repair_module
from codd.cli import main


def _write_project(tmp_path: Path, *, repair: dict | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    config: dict = {"project": {"name": "demo", "language": "python"}}
    if repair is not None:
        config["repair"] = repair
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return project


def _failure() -> object:
    return cli._verification_failure_report("verify", ["node:one"], ["failed"], {})


def _outcome(project: Path, status: str) -> SimpleNamespace:
    history_dir = project / ".codd" / "repair_history" / "2026-05-06T00-00-00Z"
    return SimpleNamespace(status=status, history_session_dir=history_dir)


def _write_failure_report(path: Path) -> Path:
    report = path / "failure_report.yaml"
    report.write_text(
        yaml.safe_dump(
            {
                "check_name": "user_journey_coherence",
                "failed_nodes": ["docs/login.md"],
                "error_messages": ["login journey failed"],
                "dag_snapshot": {"nodes": [], "edges": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return report


def _write_session(project: Path, name: str, *, status: str = "REPAIR_SUCCESS", design_doc: str = "docs/login.md") -> Path:
    session_dir = project / ".codd" / "repair_history" / name
    attempt_dir = session_dir / "attempt_0"
    attempt_dir.mkdir(parents=True)
    (session_dir / "final_status.yaml").write_text(
        yaml.safe_dump({"outcome": status, "timestamp": f"{name}-final"}, sort_keys=False),
        encoding="utf-8",
    )
    (attempt_dir / "failure_report.yaml").write_text(
        yaml.safe_dump({"check_name": "verify", "failed_nodes": [design_doc]}, sort_keys=False),
        encoding="utf-8",
    )
    (attempt_dir / "repair_proposal.yaml").write_text(
        yaml.safe_dump(
            {
                "patches": [
                    {
                        "file_path": "src/app.py",
                        "patch_mode": "full_file_replacement",
                        "content": "ok = True\n",
                    }
                ],
                "rationale": "repair failed journey",
                "confidence": 0.8,
                "proposal_timestamp": "2026-05-06T00:00:00Z",
                "rca_reference": "2026-05-06T00:00:00Z",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return session_dir


def test_verify_auto_repair_pass_exits_zero_without_repair_loop(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path)
    called = False

    monkeypatch.setattr(cli, "_run_verify_once", lambda **kwargs: cli._CliVerificationResult(True, 0, None))

    def run_loop(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cli, "_run_repair_loop", run_loop)

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 0
    assert called is False


def test_verify_auto_repair_fail_launches_repair_loop(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path, repair={"approval_mode": "required"})
    captured = {}

    monkeypatch.setattr(cli, "_run_verify_once", lambda **kwargs: cli._CliVerificationResult(False, 1, _failure()))

    def run_loop(project_root, failure, **kwargs):
        captured["project_root"] = project_root
        captured["failure"] = failure
        return _outcome(project, "REPAIR_SUCCESS")

    monkeypatch.setattr(cli, "_run_repair_loop", run_loop)

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 0
    assert captured["project_root"] == project
    assert captured["failure"].check_name == "verify"
    assert "Repair outcome: REPAIR_SUCCESS" in result.output


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        ("REPAIR_SUCCESS", 0),
        ("PARTIAL_SUCCESS", 2),
        ("MAX_ATTEMPTS_REACHED", 2),
        ("REPAIR_REJECTED_BY_HITL", 1),
        ("REPAIR_EXHAUSTED", 2),
        ("REPAIR_FAILED", 3),
    ],
)
def test_verify_auto_repair_maps_outcome_to_exit_code(tmp_path: Path, monkeypatch, status: str, exit_code: int):
    project = _write_project(tmp_path, repair={"approval_mode": "required"})
    monkeypatch.setattr(cli, "_run_verify_once", lambda **kwargs: cli._CliVerificationResult(False, 1, _failure()))
    monkeypatch.setattr(cli, "_run_repair_loop", lambda *args, **kwargs: _outcome(project, status))

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == exit_code


def test_verify_auto_repair_warns_when_repair_config_missing(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path)
    monkeypatch.setattr(cli, "_run_verify_once", lambda **kwargs: cli._CliVerificationResult(False, 1, _failure()))

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 1
    assert "WARN: codd.yaml [repair] section is required" in result.output


def test_verify_auto_repair_passes_max_attempts_and_engine(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path, repair={"approval_mode": "required"})
    captured = {}
    monkeypatch.setattr(cli, "_run_verify_once", lambda **kwargs: cli._CliVerificationResult(False, 1, _failure()))

    def run_loop(*args, **kwargs):
        captured.update(kwargs)
        return _outcome(project, "REPAIR_SUCCESS")

    monkeypatch.setattr(cli, "_run_repair_loop", run_loop)

    result = CliRunner().invoke(
        main,
        ["verify", "--path", str(project), "--auto-repair", "--max-attempts", "5", "--engine", "scripted"],
    )

    assert result.exit_code == 0
    assert captured["max_attempts"] == 5
    assert captured["engine_name"] == "scripted"


def test_verify_without_auto_repair_keeps_existing_pro_command_behavior(monkeypatch):
    captured = {}

    def run_pro(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs

    monkeypatch.setattr(cli, "_run_pro_command", run_pro)

    result = CliRunner().invoke(main, ["verify", "--path", "/tmp/demo"])

    assert result.exit_code == 0
    assert captured == {"name": "verify", "kwargs": {"path": "/tmp/demo", "sprint": None}}


def test_repair_from_report_runs_repair_loop(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path, repair={"approval_mode": "required"})
    report = _write_failure_report(tmp_path)
    captured = {}

    def run_loop(project_root, failure, **kwargs):
        captured["project_root"] = project_root
        captured["failure"] = failure
        return _outcome(project, "REPAIR_SUCCESS")

    monkeypatch.setattr(cli, "_run_repair_loop", run_loop)

    result = CliRunner().invoke(main, ["repair", "--from-report", str(report), "--path", str(project)])

    assert result.exit_code == 0
    assert captured["project_root"] == project
    assert captured["failure"].check_name == "user_journey_coherence"


def test_repair_from_report_passes_max_attempts_and_engine(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path, repair={"approval_mode": "required"})
    report = _write_failure_report(tmp_path)
    captured = {}

    def run_loop(*args, **kwargs):
        captured.update(kwargs)
        return _outcome(project, "REPAIR_SUCCESS")

    monkeypatch.setattr(cli, "_run_repair_loop", run_loop)

    result = CliRunner().invoke(
        main,
        ["repair", "--from-report", str(report), "--path", str(project), "--max-attempts", "4", "--engine", "scripted"],
    )

    assert result.exit_code == 0
    assert captured["max_attempts"] == 4
    assert captured["engine_name"] == "scripted"


def test_repair_from_report_requires_repair_config(tmp_path: Path):
    project = _write_project(tmp_path)
    report = _write_failure_report(tmp_path)

    result = CliRunner().invoke(main, ["repair", "--from-report", str(report), "--path", str(project)])

    assert result.exit_code == 1
    assert "WARN: codd.yaml [repair] section is required" in result.output


def test_run_repair_loop_configures_hybrid_classifier_context(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path, repair={"approval_mode": "required"})
    captured: dict[str, object] = {}

    class CapturingRepairLoop:
        def __init__(self, config, project_root):
            captured["config"] = config
            captured["project_root"] = project_root

        def run(self, failure, dag, **kwargs):
            captured["failure"] = failure
            captured["dag"] = dag
            captured["kwargs"] = kwargs
            return _outcome(project, "REPAIR_SUCCESS")

    monkeypatch.setattr(repair_module, "RepairLoop", CapturingRepairLoop)

    outcome = cli._run_repair_loop(
        project,
        _failure(),
        repair_config={"repair": {"approval_mode": "required"}, "ai_command": "mock-ai --json"},
        max_attempts=None,
        baseline_ref=None,
        engine_name=None,
        verify_callable=lambda: True,
    )

    config = captured["config"]
    assert outcome.status == "REPAIR_SUCCESS"
    assert captured["project_root"] == project
    assert config.repo_path == project
    assert config.llm_client.project_root == project
    assert config.llm_client.config["ai_command"] == "mock-ai --json"


def test_repair_history_lists_sessions(tmp_path: Path):
    project = _write_project(tmp_path)
    _write_session(project, "2026-05-06T00-00-00Z", status="REPAIR_SUCCESS")

    result = CliRunner().invoke(main, ["repair", "history", "--path", str(project)])

    assert result.exit_code == 0
    assert "2026-05-06T00-00-00Z\tREPAIR_SUCCESS\tattempts=1" in result.output


def test_repair_history_last_limits_newest_sessions(tmp_path: Path):
    project = _write_project(tmp_path)
    _write_session(project, "2026-05-06T00-00-00Z")
    _write_session(project, "2026-05-06T00-00-01Z")
    _write_session(project, "2026-05-06T00-00-02Z")

    result = CliRunner().invoke(main, ["repair", "history", "--path", str(project), "--last", "2"])

    assert result.exit_code == 0
    assert "2026-05-06T00-00-02Z" in result.output
    assert "2026-05-06T00-00-01Z" in result.output
    assert "2026-05-06T00-00-00Z" not in result.output


def test_repair_history_filters_by_design_doc(tmp_path: Path):
    project = _write_project(tmp_path)
    _write_session(project, "2026-05-06T00-00-00Z", design_doc="docs/login.md")
    _write_session(project, "2026-05-06T00-00-01Z", design_doc="docs/admin.md")

    result = CliRunner().invoke(
        main,
        ["repair", "history", "--path", str(project), "--design-doc", "docs/admin.md"],
    )

    assert result.exit_code == 0
    assert "2026-05-06T00-00-01Z" in result.output
    assert "2026-05-06T00-00-00Z" not in result.output


def test_repair_approve_calls_approval_helper_and_writes_approval(tmp_path: Path, monkeypatch):
    project = _write_project(tmp_path)
    session = _write_session(project, "2026-05-06T00-00-00Z")
    calls = []

    def approve(proposal, *, approval_mode, codd_yaml, notify_callable=None):
        calls.append((approval_mode, proposal.patches[0].file_path, codd_yaml["repair"]["approval_decision"]))
        return True

    monkeypatch.setattr("codd.repair.approval_repair.approve_repair_proposal", approve)

    result = CliRunner().invoke(
        main,
        ["repair", "approve", session.name, "--path", str(project)],
    )

    assert result.exit_code == 0
    assert calls == [("required", "src/app.py", "approved")]
    assert yaml.safe_load((session / "attempt_0" / "approval.yaml").read_text())["status"] == "approved"


def test_repair_status_shows_latest_session(tmp_path: Path):
    project = _write_project(tmp_path)
    _write_session(project, "2026-05-06T00-00-00Z", status="REPAIR_EXHAUSTED")
    _write_session(project, "2026-05-06T00-00-01Z", status="REPAIR_SUCCESS")

    result = CliRunner().invoke(main, ["repair", "status", "--path", str(project)])

    assert result.exit_code == 0
    assert "history_id: 2026-05-06T00-00-01Z" in result.output
    assert "status: REPAIR_SUCCESS" in result.output


def test_repair_status_accepts_history_id(tmp_path: Path):
    project = _write_project(tmp_path)
    _write_session(project, "2026-05-06T00-00-00Z", status="REPAIR_EXHAUSTED")
    _write_session(project, "2026-05-06T00-00-01Z", status="REPAIR_SUCCESS")

    result = CliRunner().invoke(
        main,
        ["repair", "status", "2026-05-06T00-00-00Z", "--path", str(project)],
    )

    assert result.exit_code == 0
    assert "history_id: 2026-05-06T00-00-00Z" in result.output
    assert "status: REPAIR_EXHAUSTED" in result.output
