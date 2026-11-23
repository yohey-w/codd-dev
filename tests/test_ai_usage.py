"""Usage metadata tests use local processes and disposable repositories only."""

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

import click
from click.testing import CliRunner
import pytest

import codd.ai_invoke as ai
import codd.ai_usage as usage
from codd.cli import main
from codd.deployment.providers.ai_command import (
    AiCommandError, AiCommandTimeout, CodexAppServerAiCommand, SubprocessAiCommand,
)
from codd.deployment.providers.codex_app_server import CodexAppServerTimeout


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("CODD_AI_USAGE_LOG", raising=False)


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def completed(stdout="answer\n", code=0, stderr=""):
    return subprocess.CompletedProcess(["fake"], code, stdout, stderr)


def test_usage_disabled_no_files_and_exact_result(tmp_path, monkeypatch):
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **kw: completed())
    assert ai.invoke_ai("fake --json", "private", project_root=tmp_path) == "answer\n"
    assert list(tmp_path.iterdir()) == []
    assert usage.resolve_usage_log({"ai": {"usage_log": None}}, tmp_path) is None


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("route", ["subprocess", "adapter"])
def test_usage_non_string_result_passes_through(tmp_path, monkeypatch, enabled, route):
    import codd.deployment.providers.ai_command_factory as factory

    result = object()
    log = tmp_path / "usage.jsonl"
    if enabled:
        monkeypatch.setenv("CODD_AI_USAGE_LOG", str(log))

    config = None
    if route == "subprocess":
        monkeypatch.setattr(ai, "_invoke_subprocess", lambda *args, **kwargs: result)
    else:
        class Adapter:
            def invoke(self, prompt):
                return result

        monkeypatch.setattr(factory, "get_ai_command", lambda *args, **kwargs: Adapter())
        config = {}

    assert ai.invoke_ai("fake", "p", config=config, project_root=tmp_path) is result
    if enabled:
        row, = records(log)
        assert row["route"] == route
        assert row["outcome"] == "success"
        assert row["output_chars"] == 0
    else:
        assert not log.exists()


def test_usage_success_privacy_and_no_output_flag_changes(tmp_path, monkeypatch):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("CODD_AI_USAGE_LOG", str(log))
    seen = []
    def run(command, **kwargs):
        seen.append((command, kwargs))
        return completed("response-secret\n")
    monkeypatch.setattr(ai.subprocess, "run", run)
    prompt = "prompt-secret あ"
    assert ai.invoke_ai("/private/fake --json --model=m1 --api-key argv-secret", prompt,
                        project_root=tmp_path, stage="generate") == "response-secret\n"
    row, = records(log)
    assert row["stage"] == "generate" and row["route"] == "subprocess"
    assert row["executable"] == "fake" and row["model"] == "m1"
    assert row["prompt_chars"] == len(prompt) and row["output_chars"] == 16
    assert row["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert row["attempt"] == 1 and row["outcome"] == "success" and row["seconds"] >= 0
    assert row["timestamp"].endswith("+00:00")
    assert seen[0][0] == ["/private/fake", "--json", "--model=m1", "--api-key", "argv-secret"]
    for secret in (prompt, "response-secret", "argv-secret", "/private/"):
        assert secret not in log.read_text()


@pytest.mark.parametrize("enabled", [False, True])
def test_usage_failure_retries_preserve_message_and_feedback(tmp_path, monkeypatch, enabled):
    log = tmp_path / "usage"
    if enabled:
        monkeypatch.setenv("CODD_AI_USAGE_LOG", str(log))
    calls = []
    def run(command, **kwargs):
        calls.append(kwargs["input"])
        return completed("partial", 1, "private failure")
    monkeypatch.setattr(ai.subprocess, "run", run)
    with pytest.raises(ValueError) as exc:
        ai.invoke_ai("fake", "p", retries=2, retry_feedback=lambda p, e, i: p + "x")
    assert type(exc.value) is ValueError and str(exc.value) == "AI command failed: private failure"
    assert calls == ["p", "px", "pxx"]
    if not enabled:
        assert not log.exists()
        return
    rows = records(log)
    assert [r["attempt"] for r in rows] == [1, 2, 3]
    assert all(r["outcome"] == "nonzero_exit" and r["output_chars"] == 7 for r in rows)
    assert "private failure" not in log.read_text()


def test_usage_auto_recovery_counts_each_trial(tmp_path, monkeypatch):
    log = tmp_path / "usage"
    monkeypatch.setenv("CODD_AI_USAGE_LOG", str(log))
    monkeypatch.setattr(ai, "TRANSIENT_BACKOFF_SECONDS", 0)
    results = iter([completed("", 1, "connection reset"), completed()])
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **kw: next(results))
    assert ai.invoke_ai("fake", "same") == "answer\n"
    rows = records(log)
    assert [r["attempt"] for r in rows] == [1, 2]
    assert rows[0]["prompt_sha256"] == rows[1]["prompt_sha256"]


@pytest.mark.parametrize("failure,message", [
    (FileNotFoundError("secret"), "AI command not found: fake"),
    (None, "AI command returned empty output"),
])
@pytest.mark.parametrize("enabled", [False, True])
def test_usage_exact_permanent_errors(tmp_path, monkeypatch, failure, message, enabled):
    log = tmp_path / "usage"
    if enabled:
        monkeypatch.setenv("CODD_AI_USAGE_LOG", str(log))
    calls = []
    def run(*args, **kwargs):
        calls.append(1)
        if failure is not None:
            raise failure
        return completed("  ")
    monkeypatch.setattr(ai.subprocess, "run", run)
    with pytest.raises(ValueError) as exc:
        ai.invoke_ai("fake", "p")
    assert type(exc.value) is ValueError and str(exc.value) == message
    if not enabled:
        assert len(calls) == 1 and not log.exists()
        return
    assert len(calls) == len(records(log)) == 1


def test_usage_writer_failure_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("CODD_AI_USAGE_LOG", str(tmp_path / "usage"))
    def broken(*args):
        raise PermissionError("secret")
    monkeypatch.setattr(usage, "_append", broken)
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **kw: completed())
    assert ai.invoke_ai("fake", "p") == "answer\n"
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **kw: completed("", 1, "original"))
    with pytest.raises(ValueError, match="^AI command failed: original$"):
        ai.invoke_ai("fake", "p")


def test_usage_path_precedence_and_outside_root(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    config = {"ai": {"usage_log": "nested/usage"}}
    assert usage.resolve_usage_log(config, root) == root / "nested/usage"
    monkeypatch.setenv("CODD_AI_USAGE_LOG", "../outside")
    assert usage.resolve_usage_log(config, root) == tmp_path / "outside"
    monkeypatch.setenv("CODD_AI_USAGE_LOG", "")
    assert usage.resolve_usage_log(config, root) is None


def test_usage_config_without_changing_subprocess_routing(tmp_path, monkeypatch):
    (tmp_path / ".codd").mkdir()
    (tmp_path / ".codd/codd.yaml").write_text("ai:\n  usage_log: nested/log\n")
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **kw: completed())
    assert ai.invoke_ai("fake", "p", project_root=tmp_path) == "answer\n"
    row, = records(tmp_path / "nested/log")
    assert row["route"] == "subprocess"


def test_usage_cwd_config_without_explicit_root_or_config(tmp_path, monkeypatch):
    (tmp_path / ".codd").mkdir()
    (tmp_path / ".codd/codd.yaml").write_text("ai:\n  usage_log: usage\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **kw: completed())
    assert ai.invoke_ai("fake", "p") == "answer\n"
    assert records(tmp_path / "usage")[0]["route"] == "subprocess"


@pytest.mark.parametrize("mode", ["success", "failure", "timeout"])
def test_usage_standalone_adapter_and_exact_errors(tmp_path, monkeypatch, mode):
    log = tmp_path / "usage"
    def run(*args, **kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired("fake", 3)
        return completed("answer\n", 1 if mode == "failure" else 0, "private")
    adapter = SubprocessAiCommand(command="/bin/fake --model m2", project_root=tmp_path,
                                  config={"ai": {"usage_log": "usage"}}, runner=run)
    if mode == "success":
        assert adapter.invoke("p", timeout=3) == "answer\n"
    else:
        kind = AiCommandTimeout if mode == "timeout" else AiCommandError
        with pytest.raises(kind) as exc:
            adapter.invoke("p", timeout=3)
        assert type(exc.value) is kind
        assert str(exc.value) == ("AI command timed out after 3s" if mode == "timeout" else
                                  "AI command failed: private")
    row, = records(log)
    assert row["route"] == "adapter_subprocess" and row["model"] == "m2"


def test_usage_invoke_adapter_does_not_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **kw: completed())
    # Dataclass runner's default holds its original function; inject a local adapter.
    import codd.deployment.providers.ai_command_factory as factory
    config = {"ai": {"usage_log": "usage"}}
    adapter = SubprocessAiCommand(command="fake", config=config, project_root=tmp_path,
                                  runner=lambda *a, **kw: completed())
    monkeypatch.setattr(factory, "get_ai_command", lambda *a, **kw: adapter)
    assert ai.invoke_ai("fake", "p", config=config, project_root=tmp_path) == "answer\n"
    assert len(records(tmp_path / "usage")) == 1


@pytest.mark.parametrize("fails", [False, True])
def test_usage_app_server_and_fallback_separate_trials(tmp_path, monkeypatch, fails):
    monkeypatch.setenv("CODD_AI_USAGE_LOG", str(tmp_path / "usage"))
    class Client:
        command = "/bin/server --private secret"
        async def start_thread(self, *args):
            return "thread"
        async def send_turn(self, *args, **kwargs):
            if fails:
                raise CodexAppServerTimeout("private failure")
            return "server result"
        async def close(self):
            pass
        async def archive_thread(self, *args):
            pass
    adapter = CodexAppServerAiCommand(Client(), project_root=tmp_path, model="m3",
        fallback_command=SubprocessAiCommand(command="fake", project_root=tmp_path,
                                            runner=lambda *a, **kw: completed()))
    try:
        assert adapter.invoke("p") == ("answer\n" if fails else "server result")
    finally:
        adapter.close()
    rows = records(tmp_path / "usage")
    assert [r["route"] for r in rows] == (["app_server", "adapter_subprocess"] if fails else ["app_server"])
    assert [r["attempt"] for r in rows] == ([1, 2] if fails else [1])
    assert rows[0]["executable"] == "server" and rows[0]["model"] == "m3"
    assert "secret" not in (tmp_path / "usage").read_text()


@pytest.mark.parametrize("invalid_destination", [False, True])
def test_usage_file_writing_real_git_repeated_log_is_never_artifact(tmp_path, monkeypatch, invalid_destination):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout
    git("init", "-q")
    source = tmp_path / "source.txt"
    source.write_text("baseline")
    git("add", "source.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline")
    # A real executable exercises process IO and git capture, without any AI service.
    executable = tmp_path / "codex-local"
    executable.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n"
                          "Path('source.txt').write_text('changed')\n"
                          "Path('new.txt').write_text('created')\n")
    executable.chmod(0o755)
    log = tmp_path if invalid_destination else tmp_path / "usage[1].jsonl"
    monkeypatch.setenv("CODD_AI_USAGE_LOG", str(log))
    for count in (1, 2):
        output = ai.invoke_ai(shlex.quote(str(executable)), "p", project_root=tmp_path)
        assert "=== FILE: source.txt ===" in output and "=== FILE: new.txt ===" in output
        assert "usage" not in output
        assert source.read_text() == "baseline" and not (tmp_path / "new.txt").exists()
        if not invalid_destination:
            assert len(records(log)) == count
        assert "usage" not in git("ls-files")
        assert git("diff", "--cached", "--name-only") == ""


@pytest.fixture
def make_git_project(tmp_path):
    def make(name, *, nested):
        repo = tmp_path / name / "repo"
        repo.mkdir(parents=True)

        def git(*args):
            return subprocess.run(
                ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
            ).stdout

        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        project = repo
        if nested:
            project = repo / "project"
            project.mkdir()
        (project / "source.txt").write_text("baseline")
        (repo / "outside.txt").write_text("baseline")
        git("add", "-A")
        git("commit", "-qm", "baseline")
        return project, git

    return make


@pytest.fixture
def nested_git_project(make_git_project):
    return make_git_project("nested", nested=True)


def run_file_writing_agent(project, source):
    executable = project.parent.parent / "codex-local"
    executable.write_text(f"#!{sys.executable}\nfrom pathlib import Path\n{source}")
    executable.chmod(0o755)
    return ai.invoke_ai(shlex.quote(str(executable)), "p", project_root=project)


def test_usage_file_writing_disabled_keeps_historical_git_argv(tmp_path, monkeypatch):
    command = ["codex-local", "--fake"]
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv == command:
            (tmp_path / "new.txt").write_text("agent addition")
        if argv == ["git", "ls-files", "--others", "--exclude-standard"]:
            return completed("new.txt\n")
        return completed("")

    monkeypatch.setattr(ai.subprocess, "run", run)

    assert ai.invoke_file_writing_agent(command, "p", tmp_path) == "=== FILE: new.txt ===\nagent addition\n"
    assert calls == [
        ["git", "add", "-A"],
        command,
        ["git", "diff", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
        ["git", "checkout", "--", "."],
        ["git", "reset", "--quiet"],
    ]
    assert not (tmp_path / "new.txt").exists()


@pytest.mark.parametrize("nested,log_location", [
    pytest.param(False, "project", id="root-log-inside-project"),
    pytest.param(True, "project", id="nested-log-inside-project"),
    pytest.param(True, "repo", id="nested-log-outside-project"),
    pytest.param(True, "outside", id="nested-log-outside-repo"),
])
def test_usage_file_writing_keeps_output_and_revert(make_git_project, monkeypatch, nested, log_location):
    def make(name):
        project, git = make_git_project(name, nested=nested)
        repo = project.parent if nested else project
        if nested:
            (project / "outside.txt").write_text("project file with the same name")
            git("add", "project/outside.txt")
            git("commit", "-qm", "project file")
            (repo / "outside.txt").write_text("user edit")
            (repo / "outside-new.txt").write_text("user addition")
        return project, repo, git

    def snapshot(project, repo, git, count):
        source = (
            "import subprocess\n"
            "staged = subprocess.check_output(['git', 'diff', '--cached', '--name-only'], text=True)\n"
            "Path('new.txt').write_text(('agent addition\\n' + staged).rstrip())\n"
            "Path('source.txt').write_text('agent change')\n"
        )
        if nested:
            source += f"Path('../outside.txt').write_text('agent outside {count}')\n"
        output = run_file_writing_agent(project, source)
        assert (project / "source.txt").read_text() == "baseline"
        assert not (project / "new.txt").exists()
        if nested:
            assert (repo / "outside.txt").read_text() == f"agent outside {count}"
            assert (repo / "outside-new.txt").read_text() == "user addition"
        return output, git("status", "--porcelain", "--untracked-files=all").splitlines()

    project, repo, git = make("disabled")
    baseline = [snapshot(project, repo, git, count) for count in (1, 2)]
    expected_output = "=== FILE: new.txt ===\nagent addition\n"
    expected_status = []
    if nested:
        # The historical diff reports repo-relative paths. From project_root,
        # project/source.txt is absent and outside.txt names the project file.
        expected_output += "outside-new.txt\noutside.txt\n\n"
        expected_output += "=== FILE: outside.txt ===\nproject file with the same name\n"
        expected_status = [" M outside.txt", "?? outside-new.txt"]
    else:
        expected_output += "\n=== FILE: source.txt ===\nagent change\n"
    assert baseline == [(expected_output, expected_status)] * 2

    project, repo, git = make("enabled")
    log_parent = {"project": project, "repo": repo, "outside": repo.parent}[log_location]
    log = log_parent / "usage[1].jsonl"
    monkeypatch.setenv("CODD_AI_USAGE_LOG", str(log))
    for count, (expected_output, expected_status) in enumerate(baseline, start=1):
        output, status = snapshot(project, repo, git, count)
        rows = records(log)
        assert [(row["route"], row["outcome"]) for row in rows] == [("file_writing", "success")] * count
        if repo in log.parents:
            # A log inside the repo is expected to appear as one untracked
            # entry. The exact remaining porcelain must match the disabled run.
            log_status = f"?? {log.relative_to(repo).as_posix()}"
            assert status.count(log_status) == 1
            status.remove(log_status)
        assert output == expected_output
        assert status == expected_status


@pytest.mark.parametrize("tracked", [False, True], ids=["untracked", "tracked"])
def test_usage_file_writing_nested_project_keeps_log_appends(nested_git_project, monkeypatch, tracked):
    project, git = nested_git_project
    relative_log = Path("nested/usage[1].jsonl")
    log = project / relative_log
    log.parent.mkdir()
    baseline = {"marker": "before"}
    appended = {"marker": "during"}
    append_line = json.dumps(appended) + "\n"
    if tracked:
        log.write_text(json.dumps(baseline) + "\n")
        git("add", "--", str(log))
        git("commit", "-qm", "log baseline")
    monkeypatch.setenv("CODD_AI_USAGE_LOG", relative_log.as_posix())

    output = run_file_writing_agent(
        project,
        f"with Path({relative_log.as_posix()!r}).open('a', encoding='utf-8') as stream:\n"
        f"    stream.write({append_line!r})\n"
        "Path('source.txt').write_text('agent change')\n"
        "Path('new.txt').write_text('agent addition')\n",
    )

    rows = records(log)
    assert rows[:-1] == ([baseline, appended] if tracked else [appended])
    assert rows[-1]["route"] == "file_writing"
    assert str(relative_log) not in output
    assert output == "=== FILE: new.txt ===\nagent addition\n"
    assert (project / "source.txt").read_text() == "baseline"
    assert not (project / "new.txt").exists()
    assert (str(log.relative_to(project.parent)) in git("ls-files").splitlines()) is tracked
    assert git("diff", "--cached", "--name-only") == ""


def test_usage_file_writing_nested_log_excludes_only_the_log(nested_git_project, monkeypatch):
    project, git = nested_git_project
    artifact_path = Path("nested/usage[1].jsonl")
    relative_log = Path(project.name) / artifact_path
    (project / relative_log).parent.mkdir(parents=True)
    (project / artifact_path).parent.mkdir()
    monkeypatch.setenv("CODD_AI_USAGE_LOG", relative_log.as_posix())

    output = run_file_writing_agent(
        project,
        f"Path({artifact_path.as_posix()!r}).write_text('agent artifact')\n"
        "Path('new.txt').write_text('agent addition')\n",
    )

    assert f"=== FILE: {artifact_path} ===\nagent artifact" in output
    assert not (project / artifact_path).exists()
    assert len(records(project / relative_log)) == 1
    assert git("diff", "--cached", "--name-only") == ""


def test_usage_file_writing_same_named_file_elsewhere_never_leaks_the_log(nested_git_project, monkeypatch):
    """A changed file whose repository-relative name equals the log's
    project-relative name must not pull the usage log into the captured output."""
    project, git = nested_git_project
    repo = project.parent
    log = project / "usage.jsonl"
    log.write_text('{"earlier": 1}\n')
    (repo / "usage.jsonl").write_text("tracked elsewhere\n")
    git("add", "usage.jsonl")
    git("commit", "-qm", "same-named file at the repository root")
    monkeypatch.setenv("CODD_AI_USAGE_LOG", "usage.jsonl")

    output = run_file_writing_agent(
        project,
        "Path('../usage.jsonl').write_text('changed by the agent\\n')\n"
        "Path('new.txt').write_text('agent addition')\n",
    )

    assert "=== FILE: new.txt ===\nagent addition" in output
    assert "usage.jsonl" not in output
    assert "earlier" not in output
    lines = records(log)
    assert lines[0] == {"earlier": 1}
    assert len(lines) == 2 and lines[1]["route"] == "file_writing"


def test_usage_file_writing_keeps_repo_wide_baseline_staging(nested_git_project, monkeypatch):
    project, git = nested_git_project
    (project.parent / "outside.txt").write_text("user edit")
    (project.parent / "outside-new.txt").write_text("user addition")
    monkeypatch.setenv("CODD_AI_USAGE_LOG", "usage.jsonl")

    output = run_file_writing_agent(
        project,
        "import subprocess\n"
        "staged = subprocess.check_output(['git', 'diff', '--cached', '--name-only'], text=True)\n"
        "Path('staged.txt').write_text(staged)\n",
    )

    assert "=== FILE: staged.txt ===\noutside-new.txt\noutside.txt\n" in output
    assert (project.parent / "outside.txt").read_text() == "user edit"
    assert (project.parent / "outside-new.txt").read_text() == "user addition"
    assert git("diff", "--cached", "--name-only") == ""


def test_usage_real_subprocess_and_cli_summary(tmp_path, monkeypatch):
    log = tmp_path / "usage"
    monkeypatch.setenv("CODD_AI_USAGE_LOG", str(log))
    command = shlex.join([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"])
    for stage, prompt in [("generate", "abc"), ("generate", "abc"), ("implement", "xy")]:
        assert ai.invoke_ai(command, prompt, stage=stage) == prompt.upper() + "\n"
    result = CliRunner().invoke(main, ["usage", "summary", "--path", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0, result.output
    groups = json.loads(result.output)
    assert groups[0]["stage"] == "generate" and groups[0]["calls"] == 2
    assert groups[0]["prompt_chars"] == 6 and groups[0]["output_chars"] == 8 and groups[0]["reissues"] == 1
    assert groups[1]["stage"] == "implement" and groups[1]["reissues"] == 0
    text = CliRunner().invoke(main, ["usage", "summary", "--log", str(log)])
    assert text.exit_code == 0 and "prompt_chars" in text.output


@pytest.mark.parametrize("config_text", [
    "ai: [private-config-value\n",
    "- private-config-value\n",
    "ai:\n  usage_log: [private-config-value]\n",
])
@pytest.mark.parametrize("use_cwd", [False, True], ids=["path", "cwd"])
def test_usage_summary_reports_malformed_config(tmp_path, monkeypatch, config_text, use_cwd):
    project = tmp_path / "project"
    (project / ".codd").mkdir(parents=True)
    (project / ".codd/codd.yaml").write_text(config_text)
    args = ["usage", "summary"]
    if use_cwd:
        monkeypatch.chdir(project)
    else:
        monkeypatch.chdir(tmp_path)
        args += ["--path", str(project)]

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 1
    assert "Error: Cannot resolve usage log" in result.output
    assert "codd.yaml" in result.output
    assert "private-config-value" not in result.output


def test_usage_summary_explicit_relative_log_uses_cwd(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / ".codd").mkdir()
    (tmp_path / ".codd/codd.yaml").write_text("ai: [malformed\n")
    (tmp_path / "usage.jsonl").write_text("")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, [
        "usage", "summary", "--path", str(project), "--log", "usage.jsonl", "--format", "json",
    ])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_usage_summary_skips_and_counts_invalid_lines(tmp_path, monkeypatch, output_format):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("CODD_AI_USAGE_LOG", str(log))
    monkeypatch.setattr(ai.subprocess, "run", lambda *args, **kwargs: completed("out"))
    ai.invoke_ai("fake", "p", stage="generate")
    good_line = log.read_bytes()
    row, = records(log)
    missing_key = {key: value for key, value in row.items() if key != "prompt_sha256"}
    wrong_type = {**row, "stage": "invalid-stage", "seconds": "private-invalid-seconds"}
    invalid_lines = [
        b"\n",
        b"\xff\n",
        json.dumps(missing_key).encode() + b"\n",
        json.dumps(wrong_type).encode() + b"\n",
        json.dumps({**row, "seconds": float("nan")}).encode() + b"\n",
        b'{"stage":',
    ]
    log.write_bytes(good_line + b"".join(invalid_lines[:-1]) + good_line + invalid_lines[-1])
    try:
        runner = CliRunner(mix_stderr=False)  # Click 8.0 and 8.1
    except TypeError:
        runner = CliRunner()

    result = runner.invoke(main, ["usage", "summary", "--format", output_format])

    assert result.exit_code == 0, result.output
    assert f"invalid_lines={len(invalid_lines)}" in result.stderr
    assert "private-invalid-seconds" not in result.stderr
    if output_format == "json":
        group, = json.loads(result.stdout)
        assert group == {"stage": "generate", "calls": 2, "prompt_chars": 2,
                         "output_chars": 6, "seconds": row["seconds"] * 2, "reissues": 1}
    else:
        header, group = result.stdout.strip().splitlines()
        assert header == "stage\tcalls\tprompt_chars\toutput_chars\tseconds\treissues"
        assert group.split("\t") == ["generate", "2", "2", "6", f"{row['seconds'] * 2:.3f}", "1"]


def test_usage_click_stage_and_unknown_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("CODD_AI_USAGE_LOG", str(tmp_path / "usage"))
    monkeypatch.setattr(ai.subprocess, "run", lambda *a, **kw: completed())
    ai.invoke_ai("fake", "p")
    @click.group()
    def cli():
        pass
    @cli.command()
    def generate():
        ai.invoke_ai("fake", "p")
    @cli.group()
    def operations():
        pass
    @operations.command("generate")
    def operations_generate():
        ai.invoke_ai("fake", "p")
        ai.invoke_ai("fake", "p", stage="explicit")
    assert CliRunner().invoke(cli, ["generate"]).exit_code == 0
    assert CliRunner().invoke(cli, ["operations", "generate"]).exit_code == 0
    assert [r["stage"] for r in records(tmp_path / "usage")] == [
        "", "generate", "operations generate", "explicit",
    ]


@pytest.mark.parametrize("fails", [False, True])
def test_usage_custom_adapter_records_without_wrapping_exception(tmp_path, monkeypatch, fails):
    import codd.deployment.providers.ai_command_factory as factory
    original = RuntimeError("private adapter failure")
    class Adapter:
        def invoke(self, prompt):
            if fails:
                raise original
            return "adapter output"
    monkeypatch.setattr(factory, "get_ai_command", lambda *a, **kw: Adapter())
    config = {"ai": {"usage_log": "usage"}}
    if fails:
        with pytest.raises(RuntimeError) as exc:
            ai.invoke_ai("fake --model m1", "p", config=config, project_root=tmp_path)
        assert exc.value is original
    else:
        assert ai.invoke_ai("fake --model m1", "p", config=config, project_root=tmp_path) == "adapter output"
    row, = records(tmp_path / "usage")
    assert row["route"] == "adapter" and row["attempt"] == 1
    assert row["outcome"] == ("RuntimeError" if fails else "success")
    assert "private" not in (tmp_path / "usage").read_text()


def test_usage_factory_app_server_honors_config_without_outer_call(tmp_path, monkeypatch):
    import codd.deployment.providers.ai_command_factory as factory
    class Client:
        command = "server"
        async def start_thread(self, *args):
            return "thread"
        async def send_turn(self, *args, **kwargs):
            return "output"
        async def archive_thread(self, *args):
            pass
        async def close(self):
            pass
    monkeypatch.setattr(factory.shutil, "which", lambda binary: "/bin/server")
    monkeypatch.setattr(CodexAppServerAiCommand, "from_config", staticmethod(
        lambda section, root: CodexAppServerAiCommand(Client(), project_root=root)))
    adapter = factory.get_ai_command({"ai": {"usage_log": "usage"}, "codex_app_server": {"enabled": True}}, tmp_path)
    try:
        assert adapter.invoke("p") == "output"
    finally:
        adapter.close()
    row, = records(tmp_path / "usage")
    assert row["route"] == "app_server"
