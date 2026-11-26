"""Exercise the speculative copy at the AI transport boundary."""

from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

import pytest
import yaml

from codd import ai_invoke, implementer
from codd.config import load_project_config
from codd.deployment.providers.ai_command import SubprocessAiCommand
from codd.greenfield.pipeline import GreenfieldPipeline, ImplementTaskRef


TARGET = "src/unit.data"
SIBLING = "src/other.fixture"
USAGE = ".codd/ai-usage.jsonl"


def put(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree(root):
    result = {}
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        value = None
        if stat.S_ISLNK(mode):
            value = os.readlink(path)
        elif stat.S_ISREG(mode):
            value = path.read_bytes()
        result[path.relative_to(root).as_posix()] = mode, value
    return result


@pytest.fixture
def case(tmp_path, monkeypatch):
    popen_init = subprocess.Popen.__init__

    def only_git(process, args, *positional, **kwargs):
        assert isinstance(args, (tuple, list)) and not kwargs.get("shell"), args
        assert Path(args[0]).name == "git" and not {"add", "commit", "push"}.intersection(args[1:]), args
        return popen_init(process, args, *positional, **kwargs)

    def unexpected_ai(*args, **kwargs):
        pytest.fail("only the fixed implementation response may be invoked")

    monkeypatch.setattr(subprocess.Popen, "__init__", only_git)
    monkeypatch.setattr(SubprocessAiCommand, "invoke", unexpected_ai)
    root = tmp_path / "project"
    config = {
        "ai_command": None,
        "ai_commands": {"implement": "fixed-writer"},
        "implement": {"owner_first_retry_on_copy": True},
        "ai": {"usage_log": USAGE},
    }
    put(root, "codd/codd.yaml", yaml.safe_dump(config).encode())
    put(root, "docs/unit.md", b"# Unit\n")
    put(root, TARGET, b"broken\n").chmod(0o751)
    put(root, SIBLING, b"original\n")
    put(root, ".codd/runtime.state", b"original\n")
    put(root, ".gitignore", b"*.fixture\n")
    (root / "relative-link").symlink_to(SIBLING)
    # An independently staged version must survive either verdict.
    def git(*args, data=None):
        return subprocess.run(["git", *args], cwd=root, input=data, capture_output=True, check=True).stdout

    git("init", "-q", "--template=")
    staged = git("hash-object", "-w", "--stdin", data=b"staged only\n").decode().strip()
    git("update-index", "--add", "--cacheinfo", f"100644,{staged},{TARGET}")
    monkeypatch.delenv("CODD_AI_USAGE_LOG", raising=False)
    monkeypatch.setattr(implementer._legacy, "_implementation_steps_context", lambda **_: None)
    owner = ImplementTaskRef("a", "docs/unit.md", ("src",), source="derived", expected_outputs=(TARGET,))
    other = replace(owner, task_id="b", expected_outputs=(SIBLING,))
    config = load_project_config(root)
    pipeline = GreenfieldPipeline(ai_command="fixed-writer")
    return root, config, [owner, other], pipeline


def exercise(case, monkeypatch, *, mutate=None, validate=None, targets=(TARGET,), **kwargs):
    from codd.greenfield.owner_first_retry import try_owner_first

    root, config, tasks, pipeline = case
    before = tree(root)
    calls = []
    checks = []

    def transport(command, prompt, *, project_root, **_):
        assert command == "fixed-writer"
        work = Path(project_root)
        assert Path.cwd() == work and work != root and work.name == root.name
        assert str(root) not in prompt and os.environ.get("PWD") == str(work)
        assert (work / ".git/index").read_bytes() == before[".git/index"][1]
        calls.append(work)
        if mutate is not None:
            mutate(work)
        return f"=== FILE: {TARGET} ===\nrepaired\n"

    def check(judge, copied_config):
        checks.append(judge)
        assert judge != calls[0] and judge.name == root.name
        if judge != root:
            assert (root / TARGET).read_bytes() == b"broken\n"
        assert (judge / ".git/index").read_bytes() == before[".git/index"][1]
        assert (judge / TARGET).read_bytes() == b"repaired\n"
        assert (judge / ".codd/runtime.state").read_bytes() == b"original\n"
        return validate(judge) if validate else True

    monkeypatch.setattr(ai_invoke, "_invoke_subprocess", transport)
    accepted = try_owner_first(
        root, tasks, targets, f"repair {root / TARGET}", config,
        reimplement=pipeline._reimplement_tasks, validate=check, **kwargs,
    )
    assert all(not path.exists() for path in calls + checks if path != root)
    after = tree(root)
    delta = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    return accepted, delta, calls, checks


def test_accept_publishes_only_checked_bytes_and_real_usage(case, monkeypatch):
    def mutate(work):
        (work / ".git/index").write_bytes(b"work index")
        put(work, ".codd/audit/reference_resolution.jsonl", b"work audit\n")

    def validate(judge):
        assert not (judge / ".codd/audit/reference_resolution.jsonl").exists()
        return True

    accepted, delta, calls, checks = exercise(case, monkeypatch, mutate=mutate, validate=validate)
    assert accepted and len(calls) == 1 and len(checks) == 2 and checks[-1] == case[0]
    root = case[0]
    assert delta == {TARGET, USAGE}
    assert (root / TARGET).read_bytes() == b"repaired\n"
    assert stat.S_IMODE((root / TARGET).stat().st_mode) == 0o751
    rows = [json.loads(line) for line in (root / USAGE).read_text().splitlines()]
    assert [row["outcome"] for row in rows if "outcome" in row] == ["success"]


@pytest.mark.parametrize("edit", ["content", "kind", "mode", "dangling", "state", "config", "ignored", "directory", "git_config"])
def test_extra_artifact_rejects_without_touching_live_tree(case, monkeypatch, edit):
    def mutate(work):
        path = work / SIBLING
        if edit == "content":
            path.write_bytes(b"partial")
        elif edit == "kind":
            path.unlink()
            path.mkdir()
        elif edit == "mode":
            path.chmod(0o711)
        elif edit == "dangling":
            (work / "new-link").symlink_to("missing")
        elif edit == "state":
            put(work, ".codd/runtime.state", b"forged")
        elif edit == "config":
            put(work, "codd/codd.yaml", b"project: {}")
        elif edit == "ignored":
            put(work, "untracked.fixture", b"extra")
        elif edit == "git_config":
            with (work / ".git/config").open("a") as stream:
                stream.write("\n[core]\n\tfilemode = false\n")
        else:
            (work / "empty").mkdir()

    accepted, delta, calls, checks = exercise(case, monkeypatch, mutate=mutate)
    assert not accepted and len(calls) == 1 and not checks
    assert delta == {USAGE}


@pytest.mark.parametrize("fault", ["exception", "timeout", "failed_check", "checker_edits_target"])
def test_failed_trial_preserves_original_and_records_call(case, monkeypatch, fault):
    def mutate(work):
        if fault == "exception":
            raise RuntimeError("failed")
        if fault == "timeout":
            raise subprocess.TimeoutExpired("fixed-writer", 1)

    def validate(judge):
        if fault == "checker_edits_target":
            (judge / TARGET).write_bytes(b"different from what was checked\n")
            return True
        return False

    accepted, delta, calls, checks = exercise(case, monkeypatch, mutate=mutate, validate=validate)
    assert not accepted and len(calls) == 1
    assert len(checks) == int(fault in {"failed_check", "checker_edits_target"})
    assert delta == {USAGE}
    assert len([row for row in (case[0] / USAGE).read_text().splitlines() if "outcome" in json.loads(row)]) == 1


@pytest.mark.parametrize("reason", [
    "directory", "directory_spelling", "link_alias", "second_link_owner", "blocked_parent", "glob", "duplicate",
    "unwritable", "misrouted", "synthesized", "outside",
])
def test_only_unique_explicit_writable_owner_may_run(case, monkeypatch, reason):
    root, _, tasks, _ = case
    if reason == "directory":
        tasks[0] = replace(tasks[0], expected_outputs=("src/",))
    elif reason == "directory_spelling":
        tasks[0] = replace(tasks[0], expected_outputs=(SIBLING, TARGET + "/"))
    elif reason == "link_alias":
        (root / "src/alias.data").symlink_to(Path(TARGET).name)
        tasks[0] = replace(tasks[0], expected_outputs=("src/alias.data",))
    elif reason == "second_link_owner":
        (root / "src/alias.data").symlink_to(Path(TARGET).name)
        tasks[1] = replace(tasks[1], expected_outputs=("src/alias.data",))
    elif reason == "blocked_parent":
        tasks[0] = replace(tasks[0], output_paths=(TARGET, SIBLING + "/nested"))
    elif reason == "glob":
        tasks[0] = replace(tasks[0], expected_outputs=("src/*.data",))
    elif reason == "duplicate":
        tasks[1] = replace(tasks[1], expected_outputs=(SIBLING, "./" + TARGET))
    elif reason == "unwritable":
        (root / TARGET).chmod(0o444)
    elif reason == "misrouted":
        tasks[0] = replace(tasks[0], output_paths=("separate",))
    elif reason == "synthesized":
        tasks[0] = replace(tasks[0], source="synthesized")
    elif reason == "outside":
        (root / "external-link").symlink_to(root.parent)
    accepted, delta, calls, checks = exercise(case, monkeypatch)
    assert not accepted and not calls and not checks and not delta


def test_judge_cannot_depend_on_a_modified_sibling(case, monkeypatch):
    def validate(judge):
        put(judge, SIBLING, b"temporarily repaired by collection")
        return True

    accepted, delta, calls, checks = exercise(case, monkeypatch, validate=validate)
    assert len(calls) == len(checks) == 1
    assert not accepted and delta == {USAGE}


@pytest.mark.parametrize("redirect", ["usage", "git", "command_parent", "command_alias"])
def test_live_path_redirections_do_not_start_the_writer(case, monkeypatch, redirect):
    root = case[0]
    options = {}
    if redirect == "usage":
        monkeypatch.setenv("CODD_AI_USAGE_LOG", "codd/codd.yaml")
    elif redirect == "git":
        location = put(root.parent, "global.cfg", f"[core]\nworktree = {root}\n".encode())
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(location))
    elif redirect == "command_parent":
        options["ai_command"] = "fixed-writer --workdir=../shared"
    else:
        alias = root.parent / "alias"
        alias.symlink_to(root)
        options["ai_command"] = f"fixed-writer --workdir={alias}"
    accepted, delta, calls, checks = exercise(case, monkeypatch, **options)
    assert not accepted and not calls and not checks and not delta


def test_publish_rollback_survives_a_second_rename_error(case, monkeypatch):
    from codd.greenfield.owner_first_retry import try_owner_first

    root, config, tasks, _ = case
    before = tree(root)
    replace_file = os.replace
    renames = []

    def write(copy, owners, *_):
        for name in (TARGET, SIBLING):
            (copy / name).write_bytes(b"replacement")
        return [task.task_id for task in owners]

    def fail(src, dst):
        renames.append((Path(src).name, Path(dst)))
        if Path(dst) == root / TARGET or Path(src).name == "old":
            raise PermissionError("replacement unavailable")
        return replace_file(src, dst)

    monkeypatch.setattr(os, "replace", fail)
    assert not try_owner_first(root, tasks, [TARGET, SIBLING], "repair", config,
                               reimplement=write, validate=lambda *_: True)
    assert renames == [("new", root / SIBLING), ("new", root / TARGET), ("old", root / SIBLING)]
    assert tree(root) == before


def test_environment_keeps_external_dependencies_and_rebases_live_aliases(case, monkeypatch):
    root = case[0]
    dependencies = root.with_name(root.name + "-deps")
    dependencies.mkdir()
    alias = root.parent / "project-alias"
    alias.symlink_to(root / "src")
    monkeypatch.setenv("SRT1_PATHS", os.pathsep.join(map(str, (dependencies, alias))))

    def inspect(copy):
        assert os.environ["SRT1_PATHS"].split(os.pathsep) == [str(dependencies), str(copy / "src")]
        return True

    accepted, delta, calls, checks = exercise(case, monkeypatch, mutate=inspect)
    assert accepted and len(calls) == 1 and len(checks) == 2 and checks[-1] == case[0]
    assert delta == {TARGET, USAGE}
    assert os.environ["SRT1_PATHS"].split(os.pathsep) == [str(dependencies), str(alias)]


def test_publication_preserves_judged_modification_time(case, monkeypatch):
    root = case[0]
    path = root / TARGET
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    timestamps = []

    def inspect(judge):
        timestamps.append((judge / TARGET).stat().st_mtime_ns)
        return True

    accepted, _, _, _ = exercise(case, monkeypatch, validate=inspect)
    assert accepted and timestamps == [path.stat().st_mtime_ns] * 2


@pytest.mark.parametrize("destination", ["default", "outer", "disabled", "existing_artifact"])
def test_usage_recording_respects_session_and_protects_inputs(case, monkeypatch, destination):
    from codd.ai_usage import usage_session

    root, config, _, _ = case
    config["ai"].pop("usage_log", None)
    outer = ".codd/run/usage.jsonl"
    if destination == "disabled":
        monkeypatch.setenv("CODD_AI_USAGE_LOG", "")
    if destination == "existing_artifact":
        put(root, "src/empty.fixture", b"")
        monkeypatch.setenv("CODD_AI_USAGE_LOG", "src/empty.fixture")
    with usage_session({"ai": {"usage_log": outer if destination == "outer" else None}}, root):
        accepted, delta, calls, checks = exercise(case, monkeypatch)
    if destination in {"disabled", "existing_artifact"}:
        assert not calls and not checks and not accepted and not delta
    else:
        assert accepted and len(calls) == 1 and len(checks) == 2 and checks[-1] == case[0]
        assert delta == ({TARGET, USAGE} if destination == "default" else {TARGET, ".codd/run", outer})


def test_cleanup_failure_after_rename_restores_original(case, monkeypatch):
    root = case[0]
    cleanup = tempfile.TemporaryDirectory.cleanup

    def fail_once(staging):
        cleanup(staging)
        if Path(staging.name).parent == (root / TARGET).parent:
            raise OSError("late cleanup error")

    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", fail_once)
    accepted, delta, _, _ = exercise(case, monkeypatch)
    assert not accepted and delta == {USAGE}


@pytest.mark.parametrize("limit", [None, "17"])
def test_copy_keeps_ordinary_ai_timeout_recovery_and_usage(case, monkeypatch, limit):
    from codd.greenfield.owner_first_retry import try_owner_first

    root, config, tasks, pipeline = case
    if limit is None:
        monkeypatch.delenv(ai_invoke.AI_CALL_TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(ai_invoke.AI_CALL_TIMEOUT_ENV, limit)
    monkeypatch.setattr(ai_invoke, "TRANSIENT_BACKOFF_SECONDS", 0)
    run = subprocess.run
    timeouts = {"copy": [], "live": []}

    def fixed_run(args, **kwargs):
        if args != ["fixed-writer"]:
            return run(args, **kwargs)
        group = timeouts["live" if Path(kwargs["cwd"]) == root else "copy"]
        group.append(kwargs["timeout"])
        if len(group) == 1:
            raise subprocess.TimeoutExpired("fixed-writer", kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0, f"=== FILE: {TARGET} ===\nrepaired\n", "")

    monkeypatch.setattr(subprocess, "run", fixed_run)
    assert try_owner_first(root, tasks, [TARGET], "repair", config,
                           reimplement=pipeline._reimplement_tasks, validate=lambda *_: True)
    assert set(pipeline._reimplement_tasks(root, tasks[:1], "repair", config)) == {"a"}
    expected = ai_invoke.resolve_ai_call_timeout()
    assert timeouts == {"copy": [expected, expected], "live": [expected, expected]}
    rows = [json.loads(line) for line in (root / USAGE).read_text().splitlines()]
    assert [row["outcome"] for row in rows if "outcome" in row] == ["timeout", "success"] * 2

@pytest.mark.parametrize("channel", ["environment", "embedded_env", "feedback", "task", "config", "command"])
def test_work_inputs_do_not_point_to_live_parent(case, monkeypatch, channel):
    from codd.greenfield.owner_first_retry import try_owner_first

    root, config, tasks, _ = case
    alias = root.parent / "workspace-link"
    alias.symlink_to(root.parent)
    parent = str(root.parent)
    feedback = "fix the target"
    command = "fixed-writer"
    if channel == "environment":
        monkeypatch.setenv("BUILD_WORKSPACE", str(alias) + "/")
    elif channel == "embedded_env":
        monkeypatch.setenv("BUILD_ARGS", f'--workspace="{alias}"')
    elif channel == "feedback":
        feedback += f' in workspace "{alias}"'
    elif channel == "task":
        tasks[0] = replace(tasks[0], description=f"Build under {parent}")
    elif channel == "config":
        config["project"]["description"] = f"Build under {alias}"
    else:
        command += f" -C{alias}"

    seen = []
    def write(copy, owners, message, settings):
        seen.append((os.environ.get("BUILD_WORKSPACE", ""), os.environ.get("BUILD_ARGS", ""),
                     message, repr(owners), repr(settings), command))
        return []

    assert not try_owner_first(root, tasks, [TARGET], feedback, config,
                               reimplement=write, validate=lambda *_: True, ai_command=command)
    assert all(parent not in str(values) and str(alias) not in str(values) for values in seen)


@pytest.mark.parametrize("fault", ["outside", "late_edit", "exception", "timeout"])
def test_live_recheck_rolls_back_every_target(case, monkeypatch, fault):
    from codd.greenfield import owner_first_retry as retry

    root, config, tasks, _ = case
    status = put(root.parent, "workspace-mode", b"stable")
    live_checks = []
    if fault == "late_edit":
        publish = retry._publish
        def edit_before_publish(*args, **kwargs):
            (root / ".codd/runtime.state").write_bytes(b"updated by concurrent editor")
            return publish(*args, **kwargs)
        monkeypatch.setattr(retry, "_publish", edit_before_publish)

    def write(copy, owners, *_):
        for name in (TARGET, SIBLING):
            (copy / name).write_bytes(b"candidate")
        return [task.task_id for task in owners]

    def check(at, settings):
        assert [(at / name).read_bytes() for name in (TARGET, SIBLING)] == [b"candidate"] * 2
        if at == root:
            live_checks.append(at)
        if fault == "outside":
            return not (at.parent / status.name).exists()
        if fault == "late_edit":
            return (at / ".codd/runtime.state").read_bytes() == b"original\n"
        if at == root:
            if fault == "timeout":
                raise subprocess.TimeoutExpired("checker", 1)
            raise ValueError("check failed")
        return True

    before = tree(root)
    assert not retry.try_owner_first(root, tasks, [TARGET, SIBLING], "repair", config,
                                     reimplement=write, validate=check)
    assert live_checks == [root]
    if fault == "late_edit":
        assert (root / ".codd/runtime.state").read_bytes() == b"updated by concurrent editor"
        before[".codd/runtime.state"] = tree(root)[".codd/runtime.state"]
    assert tree(root) == before


def test_judge_and_live_checks_inherit_ordinary_environment(case, monkeypatch):
    root = case[0]
    monkeypatch.setenv("CODD_AI_USAGE_LOG", USAGE)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(root.parent))
    monkeypatch.setenv("BUILD_WORKSPACE", str(root.parent))
    original = dict(os.environ)
    checks = []
    def check(at):
        checks.append((at, dict(os.environ)))
        return True

    accepted, _, _, _ = exercise(case, monkeypatch, validate=check)
    assert accepted
    assert len(checks) == 2 and checks[-1][0] == root
    assert all(env == original for _, env in checks)


@pytest.mark.parametrize("failure", ["write_error", "missing_row"])
def test_failed_usage_append_prevents_adoption(case, monkeypatch, failure):
    from codd import ai_usage

    original = ai_usage._append
    attempted = []
    def fail_call_only(path, record):
        if "outcome" in record:
            attempted.append(record["outcome"])
            if failure == "write_error":
                raise OSError("usage log full")
            return
        original(path, record)

    monkeypatch.setattr(ai_usage, "_append", fail_call_only)
    accepted, delta, calls, checks = exercise(case, monkeypatch)
    assert attempted == ["success"] and len(calls) == 1
    assert not accepted and not checks and TARGET not in delta

@pytest.mark.parametrize("raises", [False, True])
def test_live_checker_writes_are_rolled_back(case, monkeypatch, raises):
    root = case[0]
    def mutate_live(at):
        if at == root:
            (root / SIBLING).unlink()
            (root / SIBLING).mkdir()
            put(root, SIBLING + "/created", b"new")
            (root / ".git/index").write_bytes(b"damaged")
            (root / ".codd/runtime.state").chmod(0)
            (root / SIBLING).chmod(0)
            if raises:
                raise RuntimeError("check interrupted")
        return True

    accepted, delta, _, checks = exercise(case, monkeypatch, validate=mutate_live)
    assert not accepted and checks[-1] == root
    assert delta == {USAGE}
