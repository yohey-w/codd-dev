"""Offline regressions for the greenfield implement-oracle environment."""

import json
import os
import shlex
import subprocess
import sys
import sysconfig
import time
from dataclasses import replace
from pathlib import Path

import pytest

from codd import implement_oracle
from codd.greenfield.pipeline import (
    STATUS_DONE,
    EnvironmentProvisionError,
    GreenfieldPipeline,
    ImplementTaskRef,
    StageError,
    _OracleWriteFence,
)
from codd.implement_oracle_scope import (
    PHASE_SUPPLIER_FIRST,
    BroadRepairPlan,
    OracleRepairPhase,
    OracleRerunScope,
)
from codd.languages.adapters import env_python, oracle_python
from codd.languages.adapters.env_python import _record_env_provision_state
from codd.project_types import EnvProvisionResult, _parse_pyproject_toml
from codd.test_environment import TestEnvironmentDeclaration


_DEP = "_codd_w13_fixture_dependency"
_DISTRIBUTION = "fixture-dependency"
_PYPROJECT = """\
[project]
name = "app"
version = "0.1.0"
dependencies = []

[tool.pytest.ini_options]
addopts = "--import-mode=importlib"
pythonpath = ["src"]
"""
_SOURCE = "def value():\n    return 7\n"
_TEST = "from app.api import value\n\ndef test_value():\n    assert value() == 7\n"


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_venv(venv: Path, *, dependency: bool = True) -> Path:
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(venv)],
        check=True, capture_output=True, text=True,
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site = _venv_site(python)
    # A nested venv sees base-system packages, so also expose CoDD's installed pytest.
    _write(site, "codd-test-harness.pth", sysconfig.get_path("purelib") + "\n")
    if dependency:
        _write(site, f"{_DEP}.py", "VALUE = 7\n")
    return python


def _venv_site(python: Path) -> Path:
    return Path(subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
    ).strip())


@pytest.fixture
def python_project(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    root = tmp_path / "project with spaces"
    _write(root, "codd/codd.yaml", (
        "project: {name: app, language: python}\n"
        "implement: {python_name_lint: 'off'}\n"
    ))
    _write(root, "pyproject.toml", _PYPROJECT)
    _write(root, "src/app/__init__.py", "")
    _write(root, "src/app/api.py", _SOURCE)
    _write(root, "tests/test_api.py", _TEST)
    return root, _make_venv(root / ".venv")


def _record_environment(root: Path) -> None:
    _record_env_provision_state(root, root / ".venv", action="up_to_date", detail="fixture")


def _prepare_fixture_environment(root: Path, *, environment_root: Path, **_) -> Path:
    dependencies = _parse_pyproject_toml((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    python = _make_venv(environment_root / ".venv", dependency=_DISTRIBUTION in dependencies)
    _record_environment(environment_root)
    return python


@pytest.fixture
def collections(monkeypatch):
    runs = []
    collect = oracle_python._run_python_pytest_collect_layer

    def capture(*args, **kwargs):
        result = collect(*args, **kwargs)
        runs.append(result)
        return result

    monkeypatch.setattr(oracle_python, "_run_python_pytest_collect_layer", capture)
    return runs


def _environment_logs(messages):
    return [message for message in messages if "collecting with" in message]


@pytest.mark.parametrize("shape, exit_status", [("importlib", 2), ("conftest", 4)])
def test_collection_uses_prepared_python_or_main_fallback(
    python_project, collections, monkeypatch, shape, exit_status,
):
    root, _ = python_project
    _write(root, "pyproject.toml", _PYPROJECT.replace("[]", f'["{_DISTRIBUTION}"]'))
    if shape == "importlib":
        _write(root, "tests/test_a_constants.py", f"import {_DEP}\nSTART = {_DEP}.VALUE\n")
        _write(root, "tests/test_b_consumer.py", (
            "from tests.test_a_constants import START\n\ndef test_start():\n    assert START == 7\n"
        ))
        error = ValueError("invalid declaration\nsecond line")
    else:
        _write(root, "tests/conftest.py", f"import {_DEP}\n")
        error = EnvironmentProvisionError("cannot install dependency\nsecond line", "Check dependencies.")

    baseline = implement_oracle.run_implement_oracle_gate(
        root, language="python", project_name="app", config={"implement": {"python_name_lint": "off"}},
    )
    expected = [("test_not_collected", f"pytest_collect_exit_{exit_status}")]
    assert [(f.category, f.code) for f in baseline.findings] == expected, baseline.raw_output

    messages = []
    pipeline = GreenfieldPipeline(echo=messages.append)
    prepared = []
    monkeypatch.setattr(pipeline, "_provision_env", lambda *a, **kw: prepared.append(_prepare_fixture_environment(*a, **kw)))
    pipeline._enforce_implement_oracle_gate(root, [], {}, verdict_only=True)
    assert not collections[-1].findings
    assert _environment_logs(messages) == [
        f"[greenfield] implement-oracle: collecting with project test environment ({str(prepared[0])!r})",
    ]

    def fail_after_recording_environment(*args, **kwargs):
        _prepare_fixture_environment(*args, **kwargs)
        raise error

    # Even writing a usable isolated state before a failure must fall back.
    messages.clear()
    monkeypatch.setattr(pipeline, "_provision_env", fail_after_recording_environment)
    with pytest.raises(StageError, match="test_not_collected"):
        pipeline._enforce_implement_oracle_gate(root, [], {}, verdict_only=True)
    assert [(f.category, f.code) for f in collections[-1].findings] == expected
    reason = str(error).replace("\n", " ")
    assert _environment_logs(messages) == [
        f"[greenfield] implement-oracle: project test environment unavailable ({reason}); "
        "collecting with CoDD's interpreter",
    ]


def test_provision_failure_preserves_a_passing_oracle(python_project, monkeypatch):
    root, _ = python_project
    messages = []
    pipeline = GreenfieldPipeline(echo=messages.append)

    def fail(_root, **_):
        raise RuntimeError("dependency repair failed")

    monkeypatch.setattr(pipeline, "_provision_env", fail)
    pipeline._enforce_implement_oracle_gate(root, [], {})
    assert _environment_logs(messages) == [
        "[greenfield] implement-oracle: project test environment unavailable (dependency repair failed); "
        "collecting with CoDD's interpreter",
    ]


@pytest.mark.parametrize(("defect", "code"), [
    ("from app.api import MISSING\n", "PY_IMPORT_NAME_NOT_FOUND"),
    ("import app.missing\n", "PY_MODULE_NOT_FOUND"),
    ("def broken(:\n", "SyntaxError"),
])
def test_project_collection_keeps_real_errors(python_project, collections, monkeypatch, defect, code):
    root, _ = python_project
    _write(root, "pyproject.toml", _PYPROJECT.replace("[]", f'["{_DISTRIBUTION}"]'))
    _write(root, "tests/conftest.py", f"import {_DEP}\n")
    _write(root, "tests/test_defect.py", defect)
    messages = []
    pipeline = GreenfieldPipeline(echo=messages.append)
    prepared = []
    monkeypatch.setattr(pipeline, "_provision_env", lambda *a, **kw: prepared.append(_prepare_fixture_environment(*a, **kw)))

    with pytest.raises(StageError, match="implement-time native-oracle gate failed"):
        pipeline._enforce_implement_oracle_gate(root, [], {}, verdict_only=True)

    assert code in {finding.code for finding in collections[-1].findings}
    assert _environment_logs(messages) == [
        f"[greenfield] implement-oracle: collecting with project test environment ({str(prepared[0])!r})",
    ]


@pytest.mark.parametrize("language", ["typescript", None])
def test_other_oracles_keep_existing_environment_behavior(tmp_path, monkeypatch, language):
    messages = []
    provisions = []
    commands = []
    pipeline = GreenfieldPipeline(language=language, echo=messages.append)
    monkeypatch.setattr(pipeline, "_provision_env", provisions.append)

    def fake_run(command, **kwargs):
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        commands.append(argv)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    if language == "typescript":
        _write(tmp_path, "tsconfig.json", '{"include":["src/**/*","tests/**/*"]}')
        _write(tmp_path, "package.json", '{"name":"app","version":"0.1.0"}')
        _write(tmp_path, "src/index.ts", "export const x = 1;\n")
        _write(tmp_path, "tests/index.test.ts", "export const x = 1;\n")

    pipeline._enforce_implement_oracle_gate(tmp_path, [], {}, verdict_only=True)

    assert provisions == []
    assert _environment_logs(messages) == []
    if language == "typescript":
        assert commands == [["npm", "ci"], ["npx", "--no-install", "tsc", "--noEmit"]]
    else:
        assert commands == []
        assert any("no language declared" in message for message in messages)


@pytest.mark.parametrize("campaign", [False, True], ids=["scoped-retry", "broad-campaign"])
def test_retry_and_vb_recheck_prepare_outside_fence_before_c(python_project, monkeypatch, campaign):
    root, python = python_project
    test_path = "tests/test_api.py"
    design = "docs/design/coverage.md"
    task = ImplementTaskRef("coverage", design, (test_path,), expected_outputs=(test_path,))
    record = {"units": {task.task_id: STATUS_DONE}}
    _write(root, design, "# Test design\n")
    _write(root, "docs/test/behaviors.md", (
        "| VB | Description | Scenario |\n| --- | --- | --- |\n"
        "| VB-value | reads the value | value returns 7 |\n"
    ))
    _write(root, "tests/conftest.py", f"import {_DEP}\n")
    _write(root, test_path, _TEST.replace("import value", "import missing"))

    events = []
    prepared = []
    messages = []
    pipeline = GreenfieldPipeline(task_lister=lambda _root: [task], echo=messages.append)
    scope = OracleRerunScope("narrow", (task.task_id,), (test_path,))
    if campaign:
        phase = OracleRepairPhase(PHASE_SUPPLIER_FIRST, scope)
        plan = BroadRepairPlan((test_path,), (task.task_id,), (), (phase,))
        scope = replace(scope, rung="broad", repair_plan=plan)
    monkeypatch.setattr(implement_oracle, "_derive_scope_for_rung", lambda **_: (scope, campaign))

    def provision(project_root, *, environment_root=None, **kwargs):
        events.append("provision")
        attempt = events.count("provision")
        if environment_root is not None:
            dependency = f'["{_DISTRIBUTION}", "fixture-{attempt}"]'
            _write(project_root, "pyproject.toml", _PYPROJECT.replace("[]", dependency))
            prepared.append(_prepare_fixture_environment(project_root, environment_root=environment_root, **kwargs))
        else:
            _write(project_root, ".venv/provisioned.json", "C")
            _record_environment(project_root)

    def reimplement(
        project_root, tasks, feedback, config, *, feedback_rows=None, feedback_output_paths=(),
    ):
        assert tasks == [task]
        covered = _TEST.replace("def test_value", "# codd: covers vb=VB-value\ndef test_value")
        if feedback_output_paths:
            events.append("c-rerun")
            _write(project_root, test_path, covered)
            return
        events.append("vb-rerun" if feedback_rows else "oracle-rerun")
        _write(project_root, "src/app/out_of_scope.py", "raise AssertionError\n")
        # VB adds coverage for a test that collects but fails at runtime. C fixes it.
        _write(project_root, test_path, covered.replace("value() ==", "[value()][1] ==") if feedback_rows else _TEST)

    real_collect = oracle_python._run_python_pytest_collect_layer

    def collect(*args, **kwargs):
        events.append("collect")
        expected = python if events[-2] == "c-rerun" else prepared[-1]
        assert kwargs["interpreter"] == str(expected)
        assert expected.is_file()
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_provision_env", provision)
    monkeypatch.setattr(pipeline, "_reimplement_tasks", reimplement)
    monkeypatch.setattr(pipeline, "_checkpoint", lambda _root: None)
    monkeypatch.setattr(oracle_python, "_run_python_pytest_collect_layer", collect)

    pipeline._stage_implement(root, record, {"coverage_gate": True, "implement_test_feedback": True})

    assert events == [
        "provision", "collect", "oracle-rerun", "provision", "collect",
        "vb-rerun", "provision", "collect", "provision", "c-rerun", "collect",
    ]
    assert len(_environment_logs(messages)) == 4
    assert f'dependencies = ["{_DISTRIBUTION}", "fixture-3"]' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert len(set(prepared)) == 3 and all(not path.exists() for path in prepared)
    assert (root / ".venv/provisioned.json").read_text(encoding="utf-8") == "C"
    assert not (root / "src/app/out_of_scope.py").exists()
    assert sum("write-fence reverted 1 out-of-scope change" in line for line in messages) == 2
    (feedback_round,) = record["test_feedback"]["rounds"]
    assert feedback_round["done"] and feedback_round["environment_before"]
    assert "skipped" not in feedback_round
    (attempt,) = feedback_round["attempts"]
    assert attempt["decision"] == "accepted", attempt


@pytest.mark.parametrize("declaration", ["stale-only", "manifest", "test-strategy"])
def test_collection_builds_from_declarations_and_preserves_user_venv(
    python_project, monkeypatch, collections, declaration,
):
    root, old_python = python_project
    _write(root, "tests/conftest.py", f"import {_DEP}\n")
    if declaration == "manifest":
        _write(root, "pyproject.toml", _PYPROJECT.replace("[]", f'["{_DISTRIBUTION}"]'))
    elif declaration == "test-strategy":
        monkeypatch.setattr("codd.test_environment.read_test_environment", lambda *_: TestEnvironmentDeclaration(
            "test strategy fixture", (_DISTRIBUTION,), (),
        ))
    _record_environment(root)
    state = root / env_python._ENV_PROVISION_STATE_RELPATH
    saved_state = state.read_bytes()
    old_module = _venv_site(old_python) / f"{_DEP}.py"
    saved_module = old_module.read_bytes()
    real_run = subprocess.run
    created = []
    installs = []

    def offline_run(command, **kwargs):
        if isinstance(command, list) and command[1:3] == ["-m", "venv"] and "--without-pip" not in command:
            venv = Path(command[-1])
            assert not venv.exists()
            created.append(_make_venv(venv, dependency=False))
            return subprocess.CompletedProcess(command, 0, "", "")
        if isinstance(command, list) and command[1:4] == ["-m", "pip", "install"]:
            python = Path(command[0])
            assert python == created[-1] and Path(kwargs["cwd"]) == root
            base_install = command[-2:] == ["-e", "."]
            installs.append((python, base_install))
            deps = _parse_pyproject_toml((root / "pyproject.toml").read_text(encoding="utf-8"))
            site = _venv_site(python)
            _write(site, "editable-app.pth", str(root / "src") + "\n")
            if _DISTRIBUTION in deps["project"]["dependencies"] or command[-1] == _DISTRIBUTION:
                _write(site, f"{_DEP}.py", "VALUE = 7\n")
            return subprocess.CompletedProcess(command, 0, "", "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", offline_run)
    messages = []
    pipeline = GreenfieldPipeline(echo=messages.append)
    if declaration != "stale-only":
        pipeline._enforce_implement_oracle_gate(root, [], {}, verdict_only=True)
        assert collections[-1].findings == ()
    else:
        with pytest.raises(StageError, match="test_not_collected"):
            pipeline._enforce_implement_oracle_gate(root, [], {}, verdict_only=True)
        assert [finding.code for finding in collections[-1].findings] == ["pytest_collect_exit_4"]
    (fresh_python,) = created
    expected_installs = [(fresh_python, True)]
    if declaration == "test-strategy":
        expected_installs.append((fresh_python, False))
    assert installs == expected_installs
    assert _environment_logs(messages) == [
        f"[greenfield] implement-oracle: collecting with project test environment ({str(fresh_python)!r})",
    ]
    assert not fresh_python.exists()
    assert old_python.exists() and old_module.read_bytes() == saved_module
    assert state.read_bytes() == saved_state


@pytest.mark.parametrize("repair_outcome", ["empty", "timeout", "install-failed", "success", "changed-request"])
def test_dependency_repair_is_reused_only_within_one_gate(python_project, monkeypatch, repair_outcome):
    root, _ = python_project
    test_path = "tests/test_api.py"
    _write(root, "src/app/feature.py", f"import {_DEP}\n")
    _write(root, "docs/design/api.md", "# API\n")
    _write(root, test_path, _TEST.replace("import value", "import missing"))
    task = ImplementTaskRef("api", "docs/design/api.md", (test_path,), expected_outputs=(test_path,))
    scope = OracleRerunScope("narrow", (task.task_id,), (test_path,))
    monkeypatch.setattr(implement_oracle, "_derive_scope_for_rung", lambda **_: (scope, False))
    requests = []
    repairs_applied = []

    def build(project, _profile, manifest, imports, *, environment_root):
        assert project == root and _DEP in imports
        if _DISTRIBUTION not in manifest:
            _write(environment_root, ".venv/leftover.py", "# from the failed dependency resolution")
            return EnvProvisionResult(False, "failed", "missing runtime dependency", undeclared_imports=(_DEP,))
        assert not (environment_root / ".venv").exists()
        repairs_applied.append(environment_root)
        if repair_outcome == "install-failed":
            return EnvProvisionResult(False, "failed", "could not install the dependency")
        _prepare_fixture_environment(root, environment_root=environment_root)
        return EnvProvisionResult(True, "provisioned", "fixture prepared")

    def reply(_command, prompt, **kwargs):
        assert kwargs["recover"] is False
        requests.append(prompt)
        if repair_outcome == "timeout":
            raise ValueError("AI call timed out")
        return "[]" if repair_outcome in {"empty", "changed-request"} else json.dumps([_DISTRIBUTION])

    def reimplement(project, tasks, *_args, **_kwargs):
        assert project == root and tasks == [task]
        _write(root, test_path, _TEST)
        manifest = _PYPROJECT.replace('0.1.0', '0.1.1') if repair_outcome == "changed-request" else _PYPROJECT
        _write(root, "pyproject.toml", manifest)

    messages = []
    pipeline = GreenfieldPipeline(echo=messages.append)
    monkeypatch.setattr(env_python, "_build_python_env", build)
    monkeypatch.setattr("codd.ai_invoke.invoke_read_only", reply)
    monkeypatch.setattr(pipeline, "_reimplement_tasks", reimplement)
    pipeline._enforce_implement_oracle_gate(root, [task], {})
    expected_calls = 2 if repair_outcome == "changed-request" else 1
    assert len(requests) == expected_calls
    assert len(set(requests)) == expected_calls
    assert len(repairs_applied) == {"install-failed": 1, "success": 2}.get(repair_outcome, 0)
    assert len(set(repairs_applied)) == len(repairs_applied)
    expected_fallbacks = 0 if repair_outcome == "success" else 2
    assert sum("CoDD's interpreter" in line for line in _environment_logs(messages)) == expected_fallbacks

    _write(root, "pyproject.toml", _PYPROJECT)
    pipeline._enforce_implement_oracle_gate(root, [task], {}, verdict_only=True)
    assert len(requests) == expected_calls + 1 and requests[0] == requests[-1]


def test_oracle_dependency_request_does_not_replay_after_app_server_timeout(tmp_path, monkeypatch):
    from codd.ai_invoke import invoke_read_only
    from codd.deployment.providers.ai_command import CodexAppServerAiCommand, SubprocessAiCommand
    from codd.deployment.providers.codex_app_server import CodexAppServerTimeout

    adapter = CodexAppServerAiCommand(object())
    requests = []

    async def timed_out(prompt, **_):
        requests.append(prompt)
        raise CodexAppServerTimeout("AI call timed out")

    def fallback(*_, **__):
        pytest.fail("an identical prompt would be sent again after the timeout")

    monkeypatch.setattr("codd.deployment.providers.ai_command_factory.shutil.which", lambda _: "/fake/codex")
    monkeypatch.setattr(CodexAppServerAiCommand, "from_config", lambda *_: adapter)
    monkeypatch.setattr(SubprocessAiCommand, "invoke", fallback)
    monkeypatch.setattr(adapter, "_invoke_app_server", timed_out)
    try:
        with pytest.raises(CodexAppServerTimeout):
            invoke_read_only(
                "fake-ai", "repair dependencies", project_root=tmp_path, stage="fixture",
                config={"codex_app_server": {"enabled": True, "fallback": "subprocess"}}, recover=False,
            )
    finally:
        if adapter._loop is not None:
            adapter._loop.close()
    assert requests == ["repair dependencies"]


@pytest.mark.parametrize(("first_seconds", "second_seconds", "expected_reruns"), [(100, 0, 0), (1, 100, 1), (1, 1, 1)])
def test_broad_campaign_charges_provisioning_to_its_wall_clock(
    python_project, monkeypatch, collections, first_seconds, second_seconds, expected_reruns,
):
    root, _ = python_project
    _write(root, "codd/codd.yaml", (
        "project: {name: app, language: python}\n"
        "implement: {python_name_lint: 'off', oracle_broad_wall_clock_seconds: 100}\n"
    ))
    test_path = "tests/test_api.py"
    _write(root, "docs/design/api.md", "# API\n")
    _write(root, test_path, _TEST.replace("import value", "import missing"))
    task = ImplementTaskRef("api", "docs/design/api.md", (test_path,), expected_outputs=(test_path,))
    phase_scope = OracleRerunScope("narrow", (task.task_id,), (test_path,))
    phase = OracleRepairPhase(PHASE_SUPPLIER_FIRST, phase_scope)
    plan = BroadRepairPlan((test_path,), (task.task_id,), (), (phase,))
    scope = replace(phase_scope, rung="broad", repair_plan=plan)
    monkeypatch.setattr(implement_oracle, "_derive_scope_for_rung", lambda **_: (scope, True))
    clock = 0.0
    provisions = []
    reruns = []
    messages = []

    def provision(_project, **_):
        nonlocal clock
        provisions.append(clock)
        clock += first_seconds if len(provisions) == 1 else second_seconds
        raise EnvironmentProvisionError("offline fixture", "Use the fallback.")

    def reimplement(project, tasks, *_args, **_kwargs):
        nonlocal clock
        assert tasks == [task]
        reruns.append(clock)
        clock += 1
        _write(project, test_path, _TEST)

    pipeline = GreenfieldPipeline(echo=messages.append)
    monkeypatch.setattr(time, "monotonic", lambda: clock)
    monkeypatch.setattr(pipeline, "_provision_env", provision)
    monkeypatch.setattr(pipeline, "_reimplement_tasks", reimplement)
    exhausted = first_seconds + second_seconds >= 100
    if exhausted:
        with pytest.raises(StageError, match="native-oracle gate failed"):
            pipeline._enforce_implement_oracle_gate(root, [task], {})
    else:
        pipeline._enforce_implement_oracle_gate(root, [task], {})
    trace = (root / ".codd/oracle_repair/campaign.jsonl").read_text(encoding="utf-8").splitlines()
    event = json.loads(trace[-1])
    assert event["status"] == ("budget_exhausted" if exhausted else "passed")
    assert len(reruns) == expected_reruns
    assert len(provisions) == 1 + expected_reruns
    assert len(collections) == (1 if exhausted else 2)
    assert clock == first_seconds + expected_reruns * (1 + second_seconds)
    assert any("wall-clock budget (100s) exhausted" in message for message in messages) == exhausted


@pytest.mark.parametrize("rollback", [False, True], ids=["enforce", "rollback"])
def test_rerun_fence_does_not_read_or_restore_environment(python_project, monkeypatch, rollback):
    root, _ = python_project
    env = root / ".venv"
    environment_file = env / "lib/site-packages/example/module_000.py"
    for number in range(256):
        _write(env, f"lib/site-packages/example/module_{number:03}.py", "#" * 32768)
    outside = root / "src/app/outside.py"
    outside.write_text("original", encoding="utf-8")
    reads = []
    inside_capture = False
    real_read_bytes = Path.read_bytes
    real_capture = _OracleWriteFence._capture

    def read_bytes(path):
        if inside_capture:
            reads.append(path)
        return real_read_bytes(path)

    def capture(fence):
        nonlocal inside_capture
        inside_capture = True
        try:
            return real_capture(fence)
        finally:
            inside_capture = False

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(_OracleWriteFence, "_capture", capture)
    test_path = "tests/test_api.py"
    task = ImplementTaskRef("api", "docs/design/api.md", (test_path,), expected_outputs=(test_path,))
    scope = OracleRerunScope("narrow", (task.task_id,), (test_path,))
    pipeline = GreenfieldPipeline(echo=lambda _: None)

    def reimplement(*_args, **_kwargs):
        outside.write_text("changed", encoding="utf-8")
        environment_file.write_text("new environment content", encoding="utf-8")

    if rollback:
        from codd.greenfield.pipeline import _feedback_environment_paths

        config, language, *_ = pipeline._layout_inputs(root)
        with _OracleWriteFence(
            root, allowed_paths=(test_path,), echo=lambda _: None,
            environment_paths=_feedback_environment_paths(config, language),
        ) as fence:
            reimplement()
            fence.rollback()
    else:
        monkeypatch.setattr(pipeline, "_reimplement_tasks", reimplement)
        pipeline._rerun_tasks_with_feedback(root, [task], "fix the test", {}, scope=scope)

    assert reads.count(outside) == 2
    assert sum(path.is_relative_to(env) for path in reads) == 0
    assert outside.read_text(encoding="utf-8") == "original"
    assert environment_file.read_text(encoding="utf-8") == "new environment content"


@pytest.mark.parametrize("variable", ["PYTHONPATH", "PYTEST_ADDOPTS"])
def test_disposable_collection_ignores_ambient_import_paths(
    python_project, monkeypatch, collections, variable,
):
    root, old_python = python_project
    old_site = _venv_site(old_python)
    _write(root, "tests/conftest.py", f"from {_DEP} import VALUE\nfrom app.api import value\n")
    ambient_path = str(old_site)
    if variable == "PYTEST_ADDOPTS":
        ambient_path = shlex.join(["-o", "pythonpath=" + shlex.join(["src", ambient_path])])
    monkeypatch.setenv(variable, ambient_path)
    pipeline = GreenfieldPipeline(echo=lambda _: None)
    monkeypatch.setattr(pipeline, "_provision_env", _prepare_fixture_environment)

    with pytest.raises(StageError, match="test_not_collected"):
        pipeline._enforce_implement_oracle_gate(root, [], {}, verdict_only=True)
    assert [finding.code for finding in collections[-1].findings] == ["pytest_collect_exit_4"]

    _write(root, "pyproject.toml", _PYPROJECT.replace("[]", f'["{_DISTRIBUTION}"]'))
    pipeline._enforce_implement_oracle_gate(root, [], {}, verdict_only=True)
    assert not collections[-1].findings


@pytest.mark.parametrize("rerun_seconds", [1, 100], ids=["cap-each-operation", "already-expired"])
def test_broad_recheck_bounds_provisioning_subprocesses(python_project, monkeypatch, rerun_seconds):
    root, _ = python_project
    _write(root, "codd/codd.yaml", (
        "project: {name: app, language: python}\n"
        "implement: {python_name_lint: 'off', oracle_broad_wall_clock_seconds: 100, oracle_max_attempts: 2}\n"
    ))
    test_path = "tests/test_api.py"
    _write(root, "docs/design/api.md", "# API\n")
    _write(root, test_path, _TEST.replace("import value", "import missing"))
    task = ImplementTaskRef("api", "docs/design/api.md", (test_path,), expected_outputs=(test_path,))
    phase_scope = OracleRerunScope("narrow", (task.task_id,), (test_path,))
    plan = BroadRepairPlan(
        (test_path,), (task.task_id,), (), (OracleRepairPhase(PHASE_SUPPLIER_FIRST, phase_scope),),
    )
    scope = replace(phase_scope, rung="broad", repair_plan=plan)
    monkeypatch.setattr(implement_oracle, "_derive_scope_for_rung", lambda **_: (scope, True))
    clock = 0.0
    preparations = []
    real_run = subprocess.run

    def offline_run(command, **kwargs):
        nonlocal clock
        if isinstance(command, list) and command[1:3] in (["-m", "venv"], ["-m", "pip"]):
            operation = command[2]
            preparations.append((operation, kwargs["timeout"]))
            if len(preparations) == 1:
                elapsed = 1 if rerun_seconds == 1 else 0
                clock += elapsed
                raise subprocess.TimeoutExpired(command, elapsed)
            if operation == "venv":
                clock += 2
                return subprocess.CompletedProcess(command, 0, "", "")
            clock += kwargs["timeout"]
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, **kwargs)

    def rerun(*_args, **_kwargs):
        nonlocal clock
        clock += rerun_seconds
        _write(root, test_path, _TEST)

    pipeline = GreenfieldPipeline(echo=lambda _: None)
    monkeypatch.setattr(time, "monotonic", lambda: clock)
    monkeypatch.setattr(subprocess, "run", offline_run)
    monkeypatch.setattr(pipeline, "_reimplement_tasks", rerun)
    with pytest.raises(StageError, match="native-oracle gate failed"):
        pipeline._enforce_implement_oracle_gate(root, [task], {})
    assert clock == 100.0
    expected = [("venv", 900.0)]
    if rerun_seconds == 1:
        expected.extend([("venv", 98.0), ("pip", 96.0)])
    assert preparations == expected
    records = (root / ".codd/oracle_repair/campaign.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(records[-1])["status"] == "budget_exhausted"


def test_dependency_repair_timeout_includes_app_server_startup(monkeypatch, tmp_path):
    import asyncio

    from codd.ai_invoke import invoke_read_only
    from codd.deployment.providers.ai_command import CodexAppServerAiCommand
    from codd.deployment.providers.codex_app_server import CodexAppServerTimeout

    class OfflineClient:
        async def start_thread(self, *args):
            await asyncio.sleep(0.1)
            return "fixture-thread"

        async def send_turn(self, *args, **kwargs):
            pytest.fail("the preparation deadline should expire during startup")

    adapter = CodexAppServerAiCommand(OfflineClient(), fallback="error")
    monkeypatch.setattr("codd.deployment.providers.ai_command_factory.get_ai_command", lambda *args, **kwargs: adapter)
    try:
        with pytest.raises(CodexAppServerTimeout, match="call timed out"):
            invoke_read_only(
                "fixture-ai", "repair dependencies", project_root=tmp_path, config={}, stage="fixture",
                recover=False, timeout=0.01,
            )
    finally:
        if adapter._loop is not None:
            adapter._loop.close()


@pytest.mark.parametrize("location", ["symlink", "source-tree", "configured-file"])
def test_rerun_fence_restores_project_files_at_overlapping_environment_location(
    tmp_path, monkeypatch, location,
):
    config = {"project": {"language": "python"}}
    if location == "symlink":
        source = tmp_path / "src/app"
        source.mkdir(parents=True)
        (tmp_path / ".venv").symlink_to(source, target_is_directory=True)
    elif location == "source-tree":
        source = tmp_path / ".venv/src/app"
        config["scan"] = {"source_dirs": [".venv/src"]}
    else:
        source = tmp_path / ".venv/config"
        config["scan"] = {"config_files": [".venv/config/settings.json"]}
    name = "settings.json" if location == "configured-file" else "records.py"
    _write(source, name, "original\n")
    _write(tmp_path, "codd/codd.yaml", json.dumps(config))
    outside = source / name
    test_path = "tests/test_api.py"
    task = ImplementTaskRef("api", "docs/design/api.md", (test_path,), expected_outputs=(test_path,))
    scope = OracleRerunScope("narrow", (task.task_id,), (test_path,))
    pipeline = GreenfieldPipeline(echo=lambda _: None)
    monkeypatch.setattr(
        pipeline, "_reimplement_tasks", lambda *args, **kwargs: outside.write_text("changed", encoding="utf-8"),
    )

    pipeline._rerun_tasks_with_feedback(tmp_path, [task], "fix the test", {}, scope=scope)

    assert outside.read_text(encoding="utf-8") == "original\n"


def test_failed_provisioning_and_cleanup_still_collect_with_codd(
    python_project, monkeypatch, collections, tmp_path,
):
    import errno
    import tempfile

    root, _ = python_project
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    blocked = []
    real_chmod = os.chmod

    def provision(_root, *, environment_root, **_):
        child = environment_root / ".venv/locked"
        _write(child, "leftover", "fixture")
        child.chmod(0)
        blocked.append(child)
        raise EnvironmentProvisionError("partial installation failed", "Use the fallback.")

    def chmod(path, mode, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) in blocked:
            raise PermissionError(errno.EPERM, "fixture filesystem rejects chmod", str(path))
        return real_chmod(path, mode, *args, **kwargs)

    messages = []
    pipeline = GreenfieldPipeline(echo=messages.append)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setattr(pipeline, "_provision_env", provision)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "chmod", chmod)
            pipeline._enforce_implement_oracle_gate(root, [], {}, verdict_only=True)
        [leftover] = scratch.iterdir()
        [log] = _environment_logs(messages)
        assert "partial installation failed" in log
        assert "collecting with CoDD's interpreter" in log
        assert "fixture filesystem rejects chmod" in log and str(leftover) in log
        assert len(collections) == 1 and not collections[0].findings
    finally:
        for child in blocked:
            if child.exists():
                real_chmod(child, 0o700)
