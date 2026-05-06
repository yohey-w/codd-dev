from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

from click.testing import CliRunner
import pytest
import yaml

import codd.cli as cli
import codd.implementer as implementer_module
from codd.cli import main
from codd.implementer import TypecheckLoopResult, TypecheckRepairLoop


def _runner_sequence(*items):
    calls = []
    queue = list(items)

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return subprocess.CompletedProcess(command, item[0], item[1], item[2])

    return runner, calls


def _repair_factory(*, success: bool, call_verify: bool = True, error_message: str | None = None, captures: dict | None = None):
    class FakeRepairLoop:
        def __init__(self, config, project_root):
            self.config = config
            self.project_root = project_root

        def run(self, failure, dag, *, verify_callable):
            if captures is not None:
                captures["config"] = self.config
                captures["project_root"] = self.project_root
                captures["failure"] = failure
                captures["dag"] = dag
            if call_verify:
                verify_callable()
            attempt = SimpleNamespace(
                failure_report=failure,
                proposal=SimpleNamespace(
                    patches=[SimpleNamespace(file_path="src/service.py", patch_mode="unified_diff", content="diff")],
                    rationale="repair",
                ),
            )
            return SimpleNamespace(success=success, attempts=[attempt], error_message=error_message)

    return FakeRepairLoop


def test_result_dataclass_shape():
    result = TypecheckLoopResult("DISABLED", [], "")

    assert result.status == "DISABLED"
    assert result.attempts == []
    assert result.final_typecheck_output == ""


def test_disabled_returns_without_running_command(tmp_path: Path):
    def runner(*args, **kwargs):
        raise AssertionError("runner should not be called")

    result = TypecheckRepairLoop("check", enabled=False, runner=runner).run_after_implement(tmp_path, [], "ai")

    assert result.status == "DISABLED"


def test_enabled_requires_command(tmp_path: Path):
    with pytest.raises(ValueError, match=r"typecheck\.command"):
        TypecheckRepairLoop(None, enabled=True).run_after_implement(tmp_path, [], "ai")


def test_from_config_uses_default_max_attempts():
    loop = TypecheckRepairLoop.from_config({"typecheck": {"enabled": True, "command": "check"}})

    assert loop.max_attempts == 3


def test_from_config_reads_command_enabled_attempts_and_engine():
    loop = TypecheckRepairLoop.from_config(
        {"typecheck": {"enabled": True, "command": "check --all", "max_repair_attempts": 5, "engine": "scripted"}}
    )

    assert loop.enabled is True
    assert loop.typecheck_command == "check --all"
    assert loop.max_attempts == 5
    assert loop.engine_name == "scripted"


def test_from_config_force_enabled_overrides_disabled_config():
    loop = TypecheckRepairLoop.from_config({"typecheck": {"enabled": False, "command": "check"}}, force_enabled=True)

    assert loop.enabled is True


def test_invalid_max_attempts_falls_back_to_three():
    loop = TypecheckRepairLoop.from_config({"typecheck": {"enabled": True, "command": "check", "max_repair_attempts": 0}})

    assert loop.max_attempts == 3


def test_typecheck_pass_returns_pass_without_repair(tmp_path: Path):
    runner, calls = _runner_sequence((0, "ok\n", ""))

    result = TypecheckRepairLoop(
        "check",
        runner=runner,
        repair_loop_factory=_repair_factory(success=True, call_verify=False),
    ).run_after_implement(tmp_path, [], "ai")

    assert result.status == "PASS"
    assert result.final_typecheck_output == "ok\n"
    assert len(calls) == 1


def test_subprocess_receives_split_command_and_project_root(tmp_path: Path):
    runner, calls = _runner_sequence((0, "", ""))

    TypecheckRepairLoop("check --flag value", runner=runner).run_after_implement(tmp_path, [], "ai")

    command, kwargs = calls[0]
    assert command == ["check", "--flag", "value"]
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False


def test_stdout_and_stderr_are_preserved(tmp_path: Path):
    runner, _ = _runner_sequence((0, "out\n", "err\n"))

    result = TypecheckRepairLoop("check", runner=runner).run_after_implement(tmp_path, [], "ai")

    assert result.final_typecheck_output == "out\nerr\n"


def test_missing_executable_becomes_repair_failure_input(tmp_path: Path):
    captures: dict = {}
    runner, _ = _runner_sequence(FileNotFoundError("missing"))

    result = TypecheckRepairLoop(
        "missing-check",
        runner=runner,
        repair_loop_factory=_repair_factory(success=False, call_verify=False, captures=captures),
    ).run_after_implement(tmp_path, [Path("src/service.py")], "ai")

    assert result.status == "REPAIR_EXHAUSTED"
    assert "missing-check" in captures["failure"].error_messages[0]


def test_typecheck_fail_then_repair_success(tmp_path: Path):
    runner, calls = _runner_sequence((1, "", "bad\n"), (0, "fixed\n", ""))

    result = TypecheckRepairLoop(
        "check",
        runner=runner,
        repair_loop_factory=_repair_factory(success=True),
    ).run_after_implement(tmp_path, [Path("src/service.py")], "ai")

    assert result.status == "REPAIR_SUCCESS"
    assert result.final_typecheck_output == "fixed\n"
    assert len(calls) == 2


def test_typecheck_fail_then_repair_exhausted(tmp_path: Path):
    runner, _ = _runner_sequence((1, "", "bad\n"), (1, "", "still bad\n"))

    result = TypecheckRepairLoop(
        "check",
        runner=runner,
        repair_loop_factory=_repair_factory(success=False),
    ).run_after_implement(tmp_path, [Path("src/service.py")], "ai")

    assert result.status == "REPAIR_EXHAUSTED"
    assert result.final_typecheck_output == "still bad\n"


def test_repair_config_receives_max_attempts_and_engine(tmp_path: Path):
    captures: dict = {}
    runner, _ = _runner_sequence((1, "", "bad\n"))

    TypecheckRepairLoop(
        "check",
        max_attempts=4,
        engine_name="scripted",
        runner=runner,
        repair_loop_factory=_repair_factory(success=False, call_verify=False, captures=captures),
    ).run_after_implement(tmp_path, [], "ai")

    assert captures["config"].max_attempts == 4
    assert captures["config"].engine_name == "scripted"


def test_modified_files_are_passed_as_scope_and_dag_nodes(tmp_path: Path):
    captures: dict = {}
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("x", encoding="utf-8")
    runner, _ = _runner_sequence((1, "", "bad\n"))

    TypecheckRepairLoop(
        "check",
        runner=runner,
        repair_loop_factory=_repair_factory(success=False, call_verify=False, captures=captures),
    ).run_after_implement(tmp_path, [source], "ai")

    assert captures["failure"].failed_nodes == ["src/service.py"]
    assert list(captures["dag"].nodes) == ["src/service.py"]


def test_modified_files_are_deduplicated(tmp_path: Path):
    captures: dict = {}
    runner, _ = _runner_sequence((1, "", "bad\n"))

    TypecheckRepairLoop(
        "check",
        runner=runner,
        repair_loop_factory=_repair_factory(success=False, call_verify=False, captures=captures),
    ).run_after_implement(tmp_path, [Path("src/service.py"), tmp_path / "src" / "service.py"], "ai")

    assert captures["failure"].failed_nodes == ["src/service.py"]


def test_modified_files_outside_project_are_ignored(tmp_path: Path):
    captures: dict = {}
    runner, _ = _runner_sequence((1, "", "bad\n"))

    TypecheckRepairLoop(
        "check",
        runner=runner,
        repair_loop_factory=_repair_factory(success=False, call_verify=False, captures=captures),
    ).run_after_implement(tmp_path, [Path("/outside.py")], "ai")

    assert captures["failure"].failed_nodes == []


def test_attempts_include_typecheck_output_and_proposal(tmp_path: Path):
    runner, _ = _runner_sequence((1, "", "bad\n"), (0, "fixed\n", ""))

    result = TypecheckRepairLoop(
        "check",
        runner=runner,
        repair_loop_factory=_repair_factory(success=True),
    ).run_after_implement(tmp_path, [Path("src/service.py")], "ai")

    assert result.attempts[0]["typecheck_output"] == "bad\n"
    assert result.attempts[0]["proposal"]["patches"][0]["file_path"] == "src/service.py"


def test_repair_error_message_is_preserved(tmp_path: Path):
    runner, _ = _runner_sequence((1, "", "bad\n"))

    result = TypecheckRepairLoop(
        "check",
        runner=runner,
        repair_loop_factory=_repair_factory(success=False, call_verify=False, error_message="repair failed"),
    ).run_after_implement(tmp_path, [], "ai")

    assert result.attempts[-1] == {"error_message": "repair failed"}


def test_package_exports_typecheck_loop():
    assert implementer_module.TypecheckRepairLoop is TypecheckRepairLoop


def test_cli_without_flag_and_disabled_config_does_not_start_typecheck(tmp_path: Path, monkeypatch):
    project = _write_cli_project(tmp_path, typecheck={"enabled": False, "command": "check"})
    monkeypatch.setattr(implementer_module, "implement_tasks", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, "TypecheckRepairLoop", None, raising=False)

    result = CliRunner().invoke(main, ["implement", "run", "--path", str(project), "--task", "1-1"])

    assert result.exit_code == 0, result.output
    assert "Typecheck loop" not in result.output


def test_cli_flag_starts_typecheck_after_legacy_implement(tmp_path: Path, monkeypatch):
    project = _write_cli_project(tmp_path, typecheck={"enabled": False, "command": "check"})
    generated = project / "src" / "service.py"
    generated.parent.mkdir()
    generated.write_text("x", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        implementer_module,
        "implement_tasks",
        lambda *args, **kwargs: [SimpleNamespace(error=None, generated_files=[generated], task_id="1-1")],
    )
    monkeypatch.setattr(
        cli,
        "_run_typecheck_loop_after_implement",
        lambda **kwargs: calls.append(kwargs) or TypecheckLoopResult("PASS", [], "ok"),
    )

    result = CliRunner().invoke(main, ["implement", "run", "--path", str(project), "--task", "1-1", "--enable-typecheck-loop"])

    assert result.exit_code == 0, result.output
    assert calls[0]["force_enabled"] is True
    assert calls[0]["modified_files"] == [generated]


def test_cli_config_enabled_starts_without_flag(tmp_path: Path, monkeypatch):
    project = _write_cli_project(tmp_path, typecheck={"enabled": True, "command": "check"})
    calls = []
    monkeypatch.setattr(implementer_module, "implement_tasks", lambda *args, **kwargs: [])
    monkeypatch.setattr(cli, "_run_typecheck_loop_after_implement", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(main, ["implement", "run", "--path", str(project), "--task", "1-1"])

    assert result.exit_code == 0, result.output
    assert calls[0]["force_enabled"] is False


def test_cli_flag_missing_command_exits_with_error(tmp_path: Path, monkeypatch):
    project = _write_cli_project(tmp_path, typecheck={"enabled": False, "command": None})
    monkeypatch.setattr(implementer_module, "implement_tasks", lambda *args, **kwargs: [])

    result = CliRunner().invoke(main, ["implement", "run", "--path", str(project), "--task", "1-1", "--enable-typecheck-loop"])

    assert result.exit_code == 1
    assert "typecheck.command" in result.output


def _write_cli_project(tmp_path: Path, *, typecheck: dict | None = None) -> Path:
    project = tmp_path / "project"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    payload = {
        "project": {"name": "demo"},
        "ai_command": "ai --run",
        "scan": {"doc_dirs": ["docs/"], "source_dirs": ["src/"], "config_files": [], "exclude": []},
    }
    if typecheck is not None:
        payload["typecheck"] = typecheck
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return project
