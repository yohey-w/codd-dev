"""Exercise the verify-stage environment barrier with commands replaced at spawn."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from codd.greenfield.pipeline import (
    STAGES,
    GreenfieldPipeline,
    ImplementTaskRef,
    format_greenfield_result,
    new_session,
    save_session,
)
from codd.languages.registry import default_registry
from tests.greenfield.conftest import make_stub_project


STRATEGY = "docs/test/test_strategy.md"
STATE = ".codd/verify/exec_env.json"


def _block(packages=(), assets=()):
    yaml_text = yaml.safe_dump({"version": 1, "packages": list(packages), "assets": list(assets)})
    return f"```codd-test-environment\n{yaml_text}```\n"


def _strategy(project, body):
    path = project / STRATEGY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ncodd:\n  node_id: test:test-strategy\n---\n\n# Test Strategy\n\n" + body)
    return path


def _configure(project, language, *, acquire=None):
    config = project / "codd" / "codd.yaml"
    data = yaml.safe_load(config.read_text())
    data["project"]["language"] = language
    if acquire is not None:
        data.setdefault("verify", {})["acquire_test_assets"] = acquire
    config.write_text(yaml.safe_dump(data))


def _checkpoint(project):
    session = new_session({})
    for name in STAGES[:STAGES.index("verify")]:
        session["stages"][name]["status"] = "done"
    save_session(project, session)


class Commands:
    def __init__(self, monkeypatch):
        profile = default_registry.resolve("python")
        assets = getattr(profile.tests, "environment_assets", {})
        self.asset, self.spec = next(iter(assets.items()), (None, None))
        self.calls = []
        self.all_argv = []
        self.asset_present = False
        self.acquire_works = True
        self.acquire_error = False
        self.package_error = False
        monkeypatch.setenv("EXAMPLE_ASSET_CACHE", "/external/cache")
        real_run = subprocess.run

        def run(argv, *args, **kwargs):
            cmd = tuple(argv) if isinstance(argv, (list, tuple)) else ()
            self.all_argv.append(cmd or (str(argv),))
            if cmd[1:3] == ("-m", "venv"):
                root = Path(cmd[-1])
                bindir = root / ("Scripts" if os.name == "nt" else "bin")
                bindir.mkdir(parents=True)
                (bindir / ("python.exe" if os.name == "nt" else "python")).touch()
                self.calls.append(("venv", cmd, kwargs))
            elif cmd[1:3] == ("-I", "-c"):
                self.calls.append(("base-probe", cmd, kwargs))
            elif cmd[1:4] == ("-m", "pip", "install"):
                kind = "base-install" if cmd[-2:] == ("-e", ".") else "package-install"
                self.calls.append((kind, cmd, kwargs))
                if kind == "package-install" and self.package_error:
                    return subprocess.CompletedProcess(cmd, 1, "", "first line\n" + "x" * 2500 + "\nindex unavailable")
            elif self.spec and cmd == self.spec.check:
                self.calls.append(("asset-check", cmd, kwargs))
                return subprocess.CompletedProcess(
                    cmd, 0 if self.asset_present else 1,
                    "/external/cache/asset-bin" if self.asset_present else "",
                    "" if self.asset_present else "asset missing",
                )
            elif self.spec and cmd == self.spec.acquire:
                self.calls.append(("asset-acquire", cmd, kwargs))
                self.asset_present = self.acquire_works
                if self.acquire_error:
                    return subprocess.CompletedProcess(cmd, 2, "", "acquisition unavailable")
            elif argv == "python -m pytest --tb=short -q":
                self.calls.append(("verify-command", (argv,), kwargs))
                return subprocess.CompletedProcess(argv, 0, "1 passed", "")
            else:
                return real_run(argv, *args, **kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", run)

    @property
    def kinds(self):
        return [kind for kind, _, _ in self.calls]


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    commands = Commands(monkeypatch)
    project = make_stub_project(tmp_path, "synthetic-ai")
    _checkpoint(project)
    echo = []
    verify_calls = []

    def verify(root, **_):
        from codd.config import load_project_config
        from codd.repair.verify_runner import VerifyRunner

        verify_calls.append(root)
        config = load_project_config(root)
        executed, _, _, failure = VerifyRunner(root, config)._run_test_command(config)
        assert executed and failure is None, failure
        return "synthetic tests executed"

    def resume():
        return GreenfieldPipeline(
            coverage_gate=False, propagate_commit=False, echo=echo.append,
            verify_runner=verify, check_runner=lambda root: "checked",
        ).run(project, resume=True)

    return project, commands, verify_calls, echo, resume


def _assert_stopped(result, calls, diagnostic):
    assert result.status == "failed"
    assert result.failed_stage == "verify"
    assert not calls
    report = format_greenfield_result(result, "text")
    assert diagnostic in report
    assert "docs/test/test_strategy.md:" in report
    assert "codd greenfield --resume" in report
    assert "starting automatic repair" not in report
    return report


@pytest.mark.parametrize(("body", "reason"), [
    (_block() * 2, "only one codd-test-environment block"),
    ("```codd-test-environment\nversion: 1\n", "unclosed codd-test-environment block"),
    ("```codd-test-environment\npackages: [\n```\n", "invalid codd-test-environment YAML"),
    (_block().replace("version: 1", "version: 1\nversion: 1"), "duplicate key 'version'"),
    (_block().replace("version: 1", "version: 1\ncommand: arbitrary"), "unknown codd-test-environment field"),
    (_block().replace("version: 1\n", ""), "missing codd-test-environment field"),
    (_block().replace("version: 1", 'version: "1"'), "version must be the integer 1"),
    (_block().replace("version: 1", "version: true"), "version must be the integer 1"),
    (_block().replace("version: 1", "version: 2"), "version must be the integer 1"),
    (_block().replace("packages: []", "packages: null"), "packages must be a list"),
    (_block(assets=("not-in-profile",)), "unknown test asset 'not-in-profile'"),
])
def test_bad_declarations_stop_before_install_or_repair(scenario, body, reason):
    project, commands, verify_calls, echo, resume = scenario
    _strategy(project, body)
    report = _assert_stopped(resume(), verify_calls, reason)
    assert "test_strategy.md:8:" in report or (
        "invalid codd-test-environment YAML" in report or "only one" in report
        or "duplicate key" in report or "unclosed" in report
    )
    assert commands.kinds == []
    assert not any("repair" in line for line in echo)


@pytest.mark.parametrize("package", [
    "https://example.invalid/lib", "lib @ https://example.invalid/lib", "./lib", "/tmp/lib",
    "lib-1-py3-none-any.whl", "--index-url", "lib; echo pwned", "lib>=one", "lib ~ 1.2",
])
def test_python_rejects_invalid_package_requirements(scenario, package):
    project, commands, calls, _, resume = scenario
    _strategy(project, _block(packages=[package]))
    _assert_stopped(resume(), calls, "invalid test package requirement")
    assert not commands.calls


@pytest.mark.parametrize(("field", "reason"), [
    ("packages", "development dependencies in the project's dependency manifest"),
    ("assets", "unknown test asset"),
])
def test_other_language_declared_environment_stops(scenario, field, reason):
    project, commands, calls, _, resume = scenario
    _configure(project, "go")
    _strategy(project, _block(**{field: ["example-package"]}))
    _assert_stopped(resume(), calls, reason)
    assert not commands.calls


def test_package_install_failure_leaves_retryable_state(scenario):
    project, commands, calls, _, resume = scenario
    _strategy(project, _block(packages=["example-tools[extra]>=1.2,<2", "example-server==2.*"]))
    commands.package_error = True
    report = _assert_stopped(resume(), calls, "could not install declared test packages")
    assert "example-tools[extra]>=1.2,<2" in report
    assert "index unavailable" in report and "first line" not in report
    assert "run manually:" in report
    assert commands.kinds == ["venv", "base-install", "base-probe", "package-install"]
    _, argv, kwargs = commands.calls[-1]
    assert argv[-3:] == ("--", "example-tools[extra]>=1.2,<2", "example-server==2.*")
    assert Path(argv[0]).parents[1] == project / ".venv"
    assert kwargs["timeout"] == 900 and not kwargs.get("shell")
    assert not json.loads((project / STATE).read_text())["test_environment"]["packages_installed"]

    commands.package_error = False
    assert resume().status == "success"
    assert commands.kinds[-3:] == ["base-probe", "package-install", "verify-command"]


@pytest.mark.parametrize("acquire_setting", [None, False, "true"])
def test_missing_asset_stops_without_acquisition(scenario, acquire_setting):
    project, commands, calls, _, resume = scenario
    _configure(project, "python", acquire=acquire_setting)
    _strategy(project, _block(assets=[commands.asset]))
    report = _assert_stopped(resume(), calls, f"test asset {commands.asset!r} check failed")
    assert "asset missing" in report
    assert " ".join(commands.spec.acquire) in report
    assert "missing OS dependencies" in report
    assert "asset-acquire" not in commands.kinds
    state = json.loads((project / STATE).read_text())["test_environment"]
    assert state["asset_checks"] == {commands.asset: "failed"}


@pytest.mark.parametrize("outcome", ["ready", "missing-after-acquire", "acquisition-failed"])
def test_acquisition_rechecks_and_reports_location_or_failure(scenario, outcome):
    project, commands, calls, echo, resume = scenario
    _configure(project, "python", acquire=True)
    _strategy(project, _block(packages=["example-tools"], assets=[commands.asset]))
    commands.acquire_works = outcome == "ready"
    commands.acquire_error = outcome == "acquisition-failed"
    result = resume()
    actions = commands.kinds[4:]
    assert commands.kinds[:4] == ["venv", "base-install", "base-probe", "package-install"]
    if outcome == "ready":
        assert result.status == "success", format_greenfield_result(result, "text")
        assert actions == ["asset-check", "asset-acquire", "asset-check", "verify-command"]
        assert any("acquired test asset" in line and "/external/cache/asset-bin" in line for line in echo)
    elif outcome == "missing-after-acquire":
        _assert_stopped(result, calls, "asset missing")
        assert actions == ["asset-check", "asset-acquire", "asset-check"]
    else:
        _assert_stopped(result, calls, "acquisition unavailable")
        assert actions == ["asset-check", "asset-acquire"]
    argv_calls = commands.calls[4:]
    assert [kw["timeout"] for kind, _, kw in argv_calls if kind != "verify-command"] == (
        [60, 900] if outcome == "acquisition-failed" else [60, 900, 60]
    )
    env = argv_calls[0][2]["env"]
    assert env["PATH"].split(os.pathsep)[0] == str(Path(commands.calls[3][1][0]).parent)
    assert env["EXAMPLE_ASSET_CACHE"] == "/external/cache"
    assert all(kw["env"] == env for _, _, kw in argv_calls)
    assert all(not kw.get("shell") for kind, _, kw in argv_calls if kind != "verify-command")


def test_manifest_and_declaration_cache_and_asset_recheck(scenario):
    project, commands, calls, _, resume = scenario
    strategy = _strategy(project, _block(packages=["example-tools==1"], assets=[commands.asset]))
    commands.asset_present = True
    assert resume().status == "success"
    assert commands.kinds[-3:] == ["package-install", "asset-check", "verify-command"]

    _checkpoint(project)
    commands.asset_present = False
    calls.clear()
    commands.calls.clear()
    _assert_stopped(resume(), calls, "asset missing")
    assert commands.kinds == ["base-probe", "asset-check"]

    commands.asset_present = True
    commands.calls.clear()
    strategy.write_text(strategy.read_text().replace("example-tools==1", "example-tools==2"))
    assert resume().status == "success"
    assert commands.kinds == ["base-probe", "package-install", "asset-check", "verify-command"]
    state = json.loads((project / STATE).read_text())["test_environment"]
    assert state["declaration"] == {"packages": ["example-tools==2"], "assets": [commands.asset]}
    assert state["asset_checks"] == {commands.asset: "passed"}

    _checkpoint(project)
    commands.calls.clear()
    manifest = project / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace("[project]", '[project]\ndependencies = ["runtime-lib"]'))
    assert resume().status == "success"
    assert commands.kinds == ["base-install", "base-probe", "package-install", "asset-check", "verify-command"]

    _checkpoint(project)
    _strategy(project, _block())
    commands.calls.clear()
    assert resume().status == "success"
    assert commands.kinds == ["base-probe", "verify-command"]
    assert "test_environment" not in json.loads((project / STATE).read_text())


def pipeline_snapshot(root, language, body, monkeypatch):
    """Deterministic full-stage snapshot, also runnable against an older checkout."""
    commands = Commands(monkeypatch)
    project = make_stub_project(root, "synthetic-ai")
    _configure(project, language)
    _strategy(project, body)
    stages = []
    ai = []

    def step(name, response, *, uses_ai=False):
        def fake(*_args, **_kwargs):
            stages.append(name)
            if uses_ai:
                argv = [sys.executable, "-c", "import sys; print(sys.argv[1])", name]
                result = subprocess.run(argv, check=True, capture_output=True, text=True)
                ai.append((argv[1:], result.stdout.strip()))
            return response
        return fake

    from codd.greenfield import pipeline

    monkeypatch.setattr(pipeline, "_utc_now", lambda: "2026-01-01T00:00:00Z")
    result = GreenfieldPipeline(
        coverage_gate=False,
        elicit_runner=step("elicit", "elicit complete", uses_ai=True),
        plan_runner=step("plan", 1, uses_ai=True),
        wave_lister=lambda root: [1],
        generate_wave_runner=step("generate", "generated", uses_ai=True),
        task_lister=lambda root: [ImplementTaskRef("sample", "docs/design/example.md")],
        implement_task_runner=step("implement", "implemented", uses_ai=True),
        verify_runner=step("verify", "synthetic tests executed"),
        ci_scaffold_runner=step("ci-scaffold", "CI created"),
        propagate_runner=step("propagate", "propagated"),
        check_runner=step("check", "checked"),
    ).run(project)
    assert result.status == "success", format_greenfield_result(result, "text")
    assert len(ai) == 4 and stages[-4:] == ["verify", "ci-scaffold", "propagate", "check"]

    files = {}
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if path.is_file() and rel.parts[0] not in {".venv", ".git"} and str(rel) != STRATEGY:
            content = path.read_text().replace(str(project), "<root>")
            if rel.as_posix() == ".codd/dag.json":
                dag = json.loads(content)
                dag["built_at"] = "<time>"
                content = json.dumps(dag)
            files[rel.as_posix()] = content
    log = [[part.replace(str(project), "<root>") for part in argv] for argv in commands.all_argv]
    display = format_greenfield_result(result, "text").replace(str(project), "<root>")
    return {"stages": stages, "commands": log, "ai": ai, "result": result.status, "display": display, "files": files}


@pytest.mark.parametrize("language", ["python", "go"])
def test_empty_declaration_matches_absent_in_full_pipeline(tmp_path, monkeypatch, language):
    with monkeypatch.context() as patch:
        absent = pipeline_snapshot(tmp_path / "absent", language, "", patch)
    with monkeypatch.context() as patch:
        empty = pipeline_snapshot(tmp_path / "empty", language, _block(), patch)
    assert absent == empty


def test_only_a_body_fence_in_the_canonical_strategy_is_read(scenario):
    project, commands, _, _, resume = scenario
    malformed = _block(packages=["--bad"])
    _strategy(project, "````markdown\n" + malformed + "````\n")
    (project / "docs/test/acceptance_criteria.md").write_text(malformed)
    assert resume().status == "success"
    assert commands.kinds == ["venv", "base-install", "base-probe", "verify-command"]
