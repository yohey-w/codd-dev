from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest
import subprocess
import yaml

import codd.implementer as implementer_module
import codd.implementer.chunked_runner as chunked_module
from codd.cli import main
from codd.implementer.chunked_runner import ChunkedExecution, ChunkedRunResult, ChunkedRunner
from codd.llm.impl_step_deriver import ImplStep, ImplStepCacheRecord, impl_step_cache_path, write_impl_step_cache


class FakeProcess:
    calls: list["FakeProcess"] = []
    returncodes: list[int] = []
    outputs: list[tuple[str, str]] = []
    timeout_once = False
    interrupt_once = False

    def __init__(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        self.pid = 12345 + len(FakeProcess.calls)
        self.returncode = FakeProcess.returncodes.pop(0) if FakeProcess.returncodes else 0
        self.output = FakeProcess.outputs.pop(0) if FakeProcess.outputs else ("ok\n", "")
        self.prompts: list[str] = []
        self.communicate_count = 0
        self.terminated = False
        FakeProcess.calls.append(self)

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self.prompts.append(input)
        self.communicate_count += 1
        if FakeProcess.timeout_once and self.communicate_count == 1:
            raise subprocess.TimeoutExpired(self.command, timeout or 0, output="partial\n", stderr="late\n")
        if FakeProcess.interrupt_once and self.communicate_count == 1:
            raise KeyboardInterrupt
        return self.output

    def terminate(self):
        self.terminated = True


@pytest.fixture(autouse=True)
def reset_fake_process(monkeypatch):
    FakeProcess.calls = []
    FakeProcess.returncodes = []
    FakeProcess.outputs = []
    FakeProcess.timeout_once = False
    FakeProcess.interrupt_once = False
    monkeypatch.setattr(chunked_module.subprocess, "Popen", FakeProcess)


def _task():
    return SimpleNamespace(
        task_id="1-1",
        title="Build service",
        summary="Build service",
        module_hint="src/service.py",
        deliverable="service",
        output_dir="src/generated/service",
        task_context="context",
    )


def _steps(count: int) -> list[ImplStep]:
    return [
        ImplStep.from_dict(
            {
                "id": f"step_{index}",
                "kind": "edit",
                "rationale": f"Do step {index}",
                "source_design_section": "docs/design.md",
                "expected_outputs": [f"src/file_{index}.py"],
                "approved": True,
            }
        )
        for index in range(1, count + 1)
    ]


def _run(tmp_path: Path, *, count: int, chunk_size: int = 5):
    runner = ChunkedRunner(chunk_size=chunk_size, timeout_per_chunk=3)
    return runner.run_steps(_task(), _steps(count), "fake-ai --run", tmp_path)


def test_five_steps_make_one_chunk(tmp_path: Path):
    result = _run(tmp_path, count=5, chunk_size=5)

    assert result.status == "SUCCESS"
    assert result.total_chunks == 1
    assert len(FakeProcess.calls) == 1


def test_ten_steps_make_two_chunks(tmp_path: Path):
    result = _run(tmp_path, count=10, chunk_size=5)

    assert result.total_chunks == 2
    assert [chunk.step_ids for chunk in result.completed_chunks] == [
        ["step_1", "step_2", "step_3", "step_4", "step_5"],
        ["step_6", "step_7", "step_8", "step_9", "step_10"],
    ]


def test_one_step_make_one_chunk(tmp_path: Path):
    result = _run(tmp_path, count=1, chunk_size=5)

    assert result.total_chunks == 1
    assert result.completed_chunks[0].step_ids == ["step_1"]


def test_empty_steps_still_writes_success_history(tmp_path: Path):
    result = _run(tmp_path, count=0, chunk_size=5)

    assert result.status == "SUCCESS"
    assert result.total_chunks == 0
    assert (result.history_path / "task.yaml").is_file()
    assert (result.history_path / "final_status.yaml").is_file()


def test_chunk_size_must_be_positive():
    with pytest.raises(ValueError):
        ChunkedRunner(chunk_size=0)


def test_timeout_must_be_positive():
    with pytest.raises(ValueError):
        ChunkedRunner(timeout_per_chunk=0)


def test_execution_round_trip():
    execution = ChunkedExecution(1, ["a", "b"], "done", 1.25, 0)

    assert ChunkedExecution.from_dict(execution.to_dict()) == execution


def test_result_round_trip(tmp_path: Path):
    result = ChunkedRunResult([ChunkedExecution(0, ["a"], "done", 1.0, 0)], 1, "SUCCESS", tmp_path)

    assert ChunkedRunResult.from_dict(result.to_dict()) == result


def test_history_persists_task_chunks_and_status(tmp_path: Path):
    result = _run(tmp_path, count=6, chunk_size=5)

    assert (result.history_path / "task.yaml").is_file()
    assert (result.history_path / "chunks" / "chunk_0.yaml").is_file()
    assert (result.history_path / "chunks" / "chunk_1.yaml").is_file()
    final_status = yaml.safe_load((result.history_path / "final_status.yaml").read_text(encoding="utf-8"))
    assert final_status["status"] == "SUCCESS"


def test_progress_callback_runs_after_each_chunk(tmp_path: Path):
    calls: list[tuple[int, int]] = []
    runner = ChunkedRunner(chunk_size=2, timeout_per_chunk=3, progress_callback=lambda current, total: calls.append((current, total)))

    runner.run_steps(_task(), _steps(5), "fake-ai --run", tmp_path)

    assert calls == [(1, 3), (2, 3), (3, 3)]


def test_subprocess_receives_chunk_prompt(tmp_path: Path):
    _run(tmp_path, count=2, chunk_size=1)

    first_prompt = FakeProcess.calls[0].prompts[0]
    second_prompt = FakeProcess.calls[1].prompts[0]
    assert "step_1" in first_prompt
    assert "step_2" not in first_prompt
    assert "completed_chunks" in second_prompt
    assert "step_1" in second_prompt


def test_success_records_command_output(tmp_path: Path):
    FakeProcess.outputs = [("chunk output\n", "chunk warning\n")]

    result = _run(tmp_path, count=1)

    assert result.completed_chunks[0].ai_command_output == "chunk output\nchunk warning\n"
    assert result.completed_chunks[0].exit_code == 0


def test_nonzero_exit_returns_partial_and_stops(tmp_path: Path):
    FakeProcess.returncodes = [2, 0]

    result = _run(tmp_path, count=2, chunk_size=1)

    assert result.status == "PARTIAL"
    assert len(FakeProcess.calls) == 1


def test_timeout_returns_timeout_status(tmp_path: Path):
    FakeProcess.timeout_once = True

    result = _run(tmp_path, count=1)

    assert result.status == "TIMEOUT"
    timeout_chunk = yaml.safe_load((result.history_path / "chunks" / "chunk_0.yaml").read_text(encoding="utf-8"))
    assert timeout_chunk["timed_out"] is True


def test_timeout_sends_sigterm_to_process_group(tmp_path: Path, monkeypatch):
    sent: list[tuple[int, int]] = []
    FakeProcess.timeout_once = True
    monkeypatch.setattr(chunked_module.os, "killpg", lambda pid, sig: sent.append((pid, sig)))

    _run(tmp_path, count=1)

    assert sent == [(12345, chunked_module.signal.SIGTERM)]


def test_keyboard_interrupt_returns_user_interrupted(tmp_path: Path, monkeypatch):
    sent: list[tuple[int, int]] = []
    FakeProcess.interrupt_once = True
    monkeypatch.setattr(chunked_module.os, "killpg", lambda pid, sig: sent.append((pid, sig)))

    result = _run(tmp_path, count=1)

    assert result.status == "USER_INTERRUPTED"
    assert sent == [(12345, chunked_module.signal.SIGTERM)]


def test_resume_skips_completed_chunks(tmp_path: Path):
    runner = ChunkedRunner(chunk_size=1, timeout_per_chunk=3)
    first = runner.run_steps(_task(), _steps(1), "fake-ai --run", tmp_path)

    FakeProcess.calls = []
    result = runner.resume_steps(_task(), _steps(3), "fake-ai --run", tmp_path, first.history_path)

    assert result.status == "SUCCESS"
    assert len(FakeProcess.calls) == 2
    assert [chunk.chunk_index for chunk in result.completed_chunks] == [0, 1, 2]


def test_resume_done_history_makes_no_subprocess_calls(tmp_path: Path):
    first = _run(tmp_path, count=2, chunk_size=1)

    FakeProcess.calls = []
    result = ChunkedRunner(chunk_size=1, timeout_per_chunk=3).resume_steps(
        _task(),
        _steps(2),
        "fake-ai --run",
        tmp_path,
        first.history_path,
    )

    assert result.status == "SUCCESS"
    assert FakeProcess.calls == []


def test_resume_accepts_history_id(tmp_path: Path):
    first = _run(tmp_path, count=1)
    history_id = first.history_path.name

    result = ChunkedRunner().resume_steps(_task(), _steps(1), "fake-ai --run", tmp_path, history_id)

    assert result.history_path == first.history_path


def test_run_without_chunk_size_uses_legacy_path(tmp_path: Path, monkeypatch):
    project = _write_cli_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_implement_tasks(project_root, *, task, ai_command, use_derived_steps):
        captured.update({"project_root": project_root, "task": task, "ai_command": ai_command, "use_derived_steps": use_derived_steps})
        return []

    monkeypatch.setattr(implementer_module, "implement_tasks", fake_implement_tasks)

    result = CliRunner().invoke(main, ["implement", "run", "--path", str(project), "--task", "1-1"])

    assert result.exit_code == 0, result.output
    assert captured["task"] == "1-1"
    assert captured["use_derived_steps"] is True


def test_chunked_run_without_task_autodetects_single_task(tmp_path: Path, monkeypatch):
    project = _write_cli_project(tmp_path)
    _write_cli_step_cache(project)
    captured: dict[str, object] = {}

    def fake_run_steps(self, task, steps, ai_command, project_root):
        captured.update({"task": task, "steps": steps, "ai_command": ai_command, "project_root": project_root})
        return ChunkedRunResult([], 1, "SUCCESS", project_root / ".codd" / "chunked_run_history" / "hist")

    monkeypatch.setattr(ChunkedRunner, "run_steps", fake_run_steps)

    result = CliRunner().invoke(main, ["implement", "run", "--path", str(project), "--chunk-size", "2"])

    assert result.exit_code == 0, result.output
    assert "Auto-detected task: 1-1" in result.output
    assert captured["task"].task_id == "1-1"
    assert len(captured["steps"]) == 1


def test_chunked_run_invokes_runner_with_cached_steps(tmp_path: Path, monkeypatch):
    project = _write_cli_project(tmp_path)
    _write_cli_step_cache(project)
    captured: dict[str, object] = {}

    def fake_run_steps(self, task, steps, ai_command, project_root):
        captured.update({"task": task, "steps": steps, "ai_command": ai_command, "project_root": project_root})
        return ChunkedRunResult([], 1, "SUCCESS", project_root / ".codd" / "chunked_run_history" / "hist")

    monkeypatch.setattr(ChunkedRunner, "run_steps", fake_run_steps)

    result = CliRunner().invoke(
        main,
        ["implement", "run", "--path", str(project), "--task", "1-1", "--chunk-size", "2", "--ai-cmd", "fake-ai --run"],
    )

    assert result.exit_code == 0, result.output
    assert captured["ai_command"] == "fake-ai --run"
    assert len(captured["steps"]) == 1


def test_chunked_resume_invokes_resume_steps(tmp_path: Path, monkeypatch):
    project = _write_cli_project(tmp_path)
    _write_cli_step_cache(project)
    captured: dict[str, object] = {}

    def fake_resume_steps(self, task, steps, ai_command, project_root, history):
        captured.update({"history": history, "steps": steps})
        return ChunkedRunResult([], 1, "SUCCESS", project_root / ".codd" / "chunked_run_history" / str(history))

    monkeypatch.setattr(ChunkedRunner, "resume_steps", fake_resume_steps)

    result = CliRunner().invoke(
        main,
        ["implement", "resume", "--path", str(project), "--task", "1-1", "--history", "hist", "--ai-cmd", "fake-ai --run"],
    )

    assert result.exit_code == 0, result.output
    assert captured == {"history": "hist", "steps": captured["steps"]}
    assert len(captured["steps"]) == 1


def _write_cli_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "python"},
                "ai_command": "config-ai --run",
                "scan": {"doc_dirs": ["docs/design/"], "source_dirs": ["src/"], "config_files": [], "exclude": []},
                "implement": {"default_output_paths": {"1-1": ["src/service"]}},
                "implementer": {"approval_mode_per_step_kind": {"edit": "auto"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    design_path = project / "docs" / "design" / "service.md"
    design_path.parent.mkdir(parents=True)
    design_path.write_text(
        "---\n"
        "codd:\n"
        '  node_id: "design:service"\n'
        '  type: "design"\n'
        "---\n\n"
        "# Service Design\n\n"
        "Build service.\n",
        encoding="utf-8",
    )
    return project


def _write_cli_step_cache(project: Path) -> None:
    step = ImplStep.from_dict(
        {
            "id": "build_service",
            "kind": "edit",
            "rationale": "Build service",
            "source_design_section": "docs/design.md",
            "expected_outputs": ["src/service.py"],
            "approved": True,
        }
    )
    write_impl_step_cache(
        impl_step_cache_path("1-1", {"project_root": project}),
        ImplStepCacheRecord("fake", "key", "1-1", "doc", "template", "now", ["docs/design.md"], [step]),
    )
