"""Actual stdin/stdout subprocess cwd, independent of model/provider heuristics."""
import shlex
import subprocess
import sys

import pytest

import codd.ai_invoke as module


@pytest.mark.parametrize("supply_root", [True, False])
def test_stdout_child_cwd_matches_project_or_inherits_caller(tmp_path, monkeypatch, supply_root):
    caller = tmp_path / "caller"
    project = tmp_path / "project"
    caller.mkdir()
    project.mkdir()
    monkeypatch.chdir(caller)
    command = shlex.join([sys.executable, "-c", "import os,sys; sys.stdin.read(); print(os.getcwd())"])
    output = module.invoke_ai(command, "anonymous fixture\n", project_root=project if supply_root else None, retries=0)
    assert output.strip() == str(project if supply_root else caller)


@pytest.mark.parametrize("command", ["codex exec --full-auto -", "claude --print", "generic-agent --stdout"])
def test_hardened_stdout_uses_safe_cwd_without_enabling_capture(tmp_path, monkeypatch, command):
    project = tmp_path / "project"
    safe = tmp_path / "safe"
    project.mkdir()
    safe.mkdir()
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "fixture response", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "invoke_file_writing_agent", lambda *a, **k: pytest.fail("read-only capture must stay disabled"))
    assert module.invoke_ai(command, "fixture", project_root=project, safe_root=safe,
                            harden_read_only=True, retries=0) == "fixture response"
    assert calls[0][1]["cwd"] == str(safe)
    if command.startswith("codex"):
        assert "--full-auto" not in calls[0][0]
        assert calls[0][0][calls[0][0].index("--sandbox") + 1] == "read-only"


def test_hardened_temporary_cwd_is_used_then_cleaned(tmp_path, monkeypatch):
    from pathlib import Path
    seen = []

    def run(argv, **kwargs):
        cwd = Path(kwargs["cwd"])
        assert cwd.is_dir()
        seen.append(cwd)
        return subprocess.CompletedProcess(argv, 0, "fixture response", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    module.invoke_ai("codex exec -", "fixture", harden_read_only=True, retries=0)
    assert len(seen) == 1 and not seen[0].exists()
