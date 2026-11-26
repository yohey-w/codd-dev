"""Run the real oracle and verify pytest commands against the same test tree."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import pytest

from codd.dag import DAG, Node
from codd.implement_oracle import (
    PythonOracleScope,
    _run_python_pytest_collect_layer,
    run_implement_oracle_gate,
)
from codd.repair import verify_runner


def _write(root: Path, relative: str, content: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


@pytest.fixture
def python_project(tmp_path: Path, monkeypatch):
    root = tmp_path / "project"
    _write(root, "src/sample/__init__.py", "VALUE = 17\n")
    _write(root, "tests/__init__.py", "")
    _write(root, "tests/e2e/helpers/__init__.py", "from tests.e2e.helpers.values import actual\n")
    _write(root, "tests/e2e/helpers/values.py", "from sample import VALUE as actual\n")
    test = "tests/e2e/helpers/test_values.py"
    _write(root, test, "from tests.e2e.helpers import actual\n\ndef test_value():\n    assert actual == 17\n")
    _write(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src"]\n'
        'addopts = "--import-mode=importlib -p no:cacheprovider"\n',
    )
    config = {"project": {"language": "python", "name": "sample"},
              "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]}}
    _write(root, "codd/codd.yaml", json.dumps(config))

    # Make pytest available to a distinct validation interpreter without pip.
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root / ".venv")
    bin_dir = root / ".venv/bin"
    site_packages = sysconfig.get_path("purelib", vars={"base": str(root / ".venv")})
    _write(Path(site_packages), "fixture-dependencies.pth", sysconfig.get_path("purelib") + "\n")
    entry = _write(bin_dir, "pytest_entry.py", "from pytest import console_main\nraise SystemExit(console_main())\n")
    console_command = shlex.join([str(bin_dir / "python"), str(entry)])
    console = _write(bin_dir, "pytest", f'#!/bin/sh\nexec {console_command} "$@"\n')
    console.chmod(0o755)
    _write(root, ".codd/verify/exec_env.json", json.dumps({"path_prepend_dirs": [str(bin_dir)]}))
    for key in ("PYTHONPATH", "PYTHONSAFEPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PATH", str(tmp_path / "unprovisioned"))

    dag = DAG()
    dag.add_node(Node("verification:e2e:values", "verification_test", attributes={
        "kind": "e2e", "template_ref": "pytest_http", "source": test,
    }))
    monkeypatch.setattr(verify_runner, "build_dag", lambda *_args: dag)
    monkeypatch.setattr(verify_runner, "run_checks", lambda *_args, **_kwargs: [])
    return root, config, test


def test_python_oracle_and_both_verify_surfaces_use_module_invocation(python_project, monkeypatch):
    root, config, test = python_project
    calls = []
    run = subprocess.run

    def observe(command, *args, **kwargs):
        if isinstance(command, str) and "pytest" in command:
            calls.append((command, kwargs["cwd"], kwargs.get("env")))
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe)
    oracle = run_implement_oracle_gate(
        root, language="python", source_dirs=["src"], test_dirs=["tests"],
        project_name="sample", config=config,
    )
    assert oracle.executed and oracle.passed and not oracle.findings
    assert "1 test collected" in oracle.raw_output
    assert shlex.split(calls[0][0]) == [
        sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider",
    ]

    result = verify_runner.run_standalone_verify(root)
    assert result.runtime_results[0]["command"] == f"python -m pytest -q {test}"
    assert result.runtime_results[0]["passed"], result.runtime_results
    assert "1 passed" in result.runtime_results[0]["output"]
    assert not (root / "tests/e2e/__init__.py").exists()
    assert result.passed, result.failures
    assert result.tests_executed and "1 passed" in result.tests_summary
    assert result.fallback_reason == "missing_adapter_legacy_compatible"
    assert shlex.split(result.test_command)[:3] == ["python", "-m", "pytest"]
    assert len(calls) == 3
    assert all(cwd == root for _command, cwd, _env in calls)
    assert all(env["PATH"].split(os.pathsep)[0] == str(root / ".venv/bin")
               for _command, _cwd, env in calls[1:])


def test_reexported_python_collector_accepts_the_original_arguments(python_project):
    root, config, test = python_project
    scope = PythonOracleScope(("src/sample/__init__.py",), (test,))

    collected = _run_python_pytest_collect_layer(root, "src", "src/sample", "tests", scope, config)

    assert collected.executed and not collected.findings
    assert "1 test collected" in collected.output


def test_python_verify_honors_explicit_console_command(python_project):
    root, config, _test = python_project
    command = "pytest --tb=short -q"
    result = verify_runner.run_standalone_verify(root, {**config, "verify": {"test_command": command}})

    assert result.test_command == command
    assert result.fallback_reason == "explicit_test_command"
    observation = next(item for item in result.observations if item.check_name == "test_command")
    assert observation.exit_code == 2
    assert "No module named 'tests'" in observation.stdout + observation.stderr
