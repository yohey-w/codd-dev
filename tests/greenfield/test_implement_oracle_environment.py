"""Offline regressions for the greenfield implement-oracle environment."""

import os
import shlex
import subprocess
import sys
import sysconfig
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
)
from codd.implement_oracle_scope import (
    PHASE_SUPPLIER_FIRST,
    BroadRepairPlan,
    OracleRepairPhase,
    OracleRerunScope,
)
from codd.languages.adapters import oracle_python
from codd.languages.adapters.env_python import _record_env_provision_state


_DEP = "_codd_w13_fixture_dependency"
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
    venv = root / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(venv)],
        check=True, capture_output=True, text=True,
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site = Path(subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
    ).strip())
    # A nested venv sees base-system packages, so also expose CoDD's installed pytest.
    _write(site, "codd-test-harness.pth", sysconfig.get_path("purelib") + "\n")
    _write(site, f"{_DEP}.py", "VALUE = 7\n")
    return root, python


def _record_environment(root: Path) -> None:
    _record_env_provision_state(root, root / ".venv", action="up_to_date", detail="fixture")


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
    root, python = python_project
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
    monkeypatch.setattr(pipeline, "_provision_env", _record_environment)
    pipeline._enforce_implement_oracle_gate(root, [], {}, verdict_only=True)
    assert not collections[-1].findings
    assert _environment_logs(messages) == [
        f"[greenfield] implement-oracle: collecting with project test environment ({str(python)!r})",
    ]

    def fail_after_recording_environment(_root):
        raise error

    # The previous state still points to a working interpreter. A failed prepare
    # must ignore it and reproduce main's verdict and diagnostic.
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

    def fail(_root):
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
    root, python = python_project
    _write(root, "tests/conftest.py", f"import {_DEP}\n")
    _write(root, "tests/test_defect.py", defect)
    messages = []
    pipeline = GreenfieldPipeline(echo=messages.append)
    monkeypatch.setattr(pipeline, "_provision_env", _record_environment)

    with pytest.raises(StageError, match="implement-time native-oracle gate failed"):
        pipeline._enforce_implement_oracle_gate(root, [], {}, verdict_only=True)

    assert code in {finding.code for finding in collections[-1].findings}
    assert _environment_logs(messages) == [
        f"[greenfield] implement-oracle: collecting with project test environment ({str(python)!r})",
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
    messages = []
    pipeline = GreenfieldPipeline(task_lister=lambda _root: [task], echo=messages.append)
    scope = OracleRerunScope("narrow", (task.task_id,), (test_path,))
    if campaign:
        phase = OracleRepairPhase(PHASE_SUPPLIER_FIRST, scope)
        plan = BroadRepairPlan((test_path,), (task.task_id,), (), (phase,))
        scope = replace(scope, rung="broad", repair_plan=plan)
    monkeypatch.setattr(implement_oracle, "_derive_scope_for_rung", lambda **_: (scope, campaign))

    def provision(project_root):
        events.append("provision")
        attempt = events.count("provision")
        # C's later prepare reuses the third evaluation's environment.
        if attempt != 4:
            dependency = f'["fixture-{attempt}"]'
            _write(project_root, "pyproject.toml", _PYPROJECT.replace("[]", dependency))
            _write(project_root, ".venv/provisioned.json", str(attempt))
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
        assert kwargs["interpreter"] == str(python)
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
    assert 'dependencies = ["fixture-3"]' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert (root / ".venv/provisioned.json").read_text(encoding="utf-8") == "3"
    assert not (root / "src/app/out_of_scope.py").exists()
    assert sum("write-fence reverted 1 out-of-scope change" in line for line in messages) == 2
    (feedback_round,) = record["test_feedback"]["rounds"]
    assert feedback_round["done"] and feedback_round["environment_before"]
    assert "skipped" not in feedback_round
    (attempt,) = feedback_round["attempts"]
    assert attempt["decision"] == "accepted", attempt
