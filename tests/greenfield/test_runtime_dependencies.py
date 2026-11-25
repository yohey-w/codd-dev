"""Offline regressions for dependency provisioning in the verify stage."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import packaging
import pytest
import yaml

from codd import project_types
from codd.greenfield.pipeline import (
    STAGES,
    GreenfieldPipeline,
    _default_ci_scaffold_runner,
    format_greenfield_result,
    new_session,
    save_session,
)


_BASELINE_MANIFEST = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "demo"
version = "0.0.0"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
addopts = "-p no:cacheprovider --import-mode=importlib"
"""


def _project(root: Path, *, external: bool = True) -> Path:
    (root / "codd").mkdir(parents=True)
    (root / "codd" / "codd.yaml").write_text("project:\n  name: demo\n  language: python\n")
    package = root / "src" / "demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from sample_lib import VALUE\n" if external else "from math import pi\n"
    )
    return root


def _checkpoint(root: Path, *, failed: bool = False) -> None:
    session = new_session({})
    for stage in STAGES[:STAGES.index("verify")]:
        session["stages"][stage]["status"] = "done"
    if failed:
        session["stages"]["verify"].update(status="failed", detail="previous provisioning failure")
        session["result"].update(status="failed", failed_stage="verify")
    save_session(root, session)


def _fake_ai(tmp_path: Path, response: list[str]) -> tuple[str, Path]:
    calls = tmp_path / "ai-calls.jsonl"
    script = tmp_path / "fake_ai.py"
    script.write_text(
        "import json, pathlib, sys\n"
        f"with pathlib.Path({str(calls)!r}).open('a') as log:\n"
        "    log.write(json.dumps(sys.stdin.read()) + '\\n')\n"
        f"print({json.dumps(response)!r})\n"
    )
    return shlex.join([sys.executable, str(script)]), calls


@pytest.fixture
def offline_install(monkeypatch):
    """Run a real isolated import probe, with a file-only stand-in for pip."""
    real_run = subprocess.run
    installs = []

    def run(argv, *args, **kwargs):
        if isinstance(argv, list) and argv[1:4] == ["-m", "pip", "install"]:
            root = Path(kwargs["cwd"])
            interpreter = argv[0]
            site = Path(real_run(
                [interpreter, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                check=True, capture_output=True, text=True,
            ).stdout.strip())
            manifest = project_types._parse_pyproject_toml((root / "pyproject.toml").read_text())
            deps = manifest["project"].get("dependencies", [])
            installs.append((argv, deps))
            (site / "pytest.py").write_text("")
            shutil.copytree(Path(packaging.__file__).parent, site / "packaging", dirs_exist_ok=True)
            (site / "demo.pth").write_text(str(root / "src") + "\n")
            metadata = site / "demo-0.0.0.dist-info"
            metadata.mkdir(exist_ok=True)
            (metadata / "METADATA").write_text(
                "Name: demo\nVersion: 0.0.0\n"
                + "".join(f"Requires-Dist: {dep}\n" for dep in deps)
            )
            if "sample-distribution" in deps:
                (site / "sample_lib.py").write_text("VALUE = 42\n")
                metadata = site / "sample_distribution-1.0.dist-info"
                metadata.mkdir(exist_ok=True)
                (metadata / "METADATA").write_text("Name: sample-distribution\nVersion: 1.0\n")
                (metadata / "top_level.txt").write_text("sample_lib\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(project_types.subprocess, "run", run)
    return installs


def _pipeline(command: str, **kwargs) -> GreenfieldPipeline:
    return GreenfieldPipeline(
        ai_command=command,
        coverage_gate=False,
        max_repair_attempts=1,
        propagate_commit=False,
        verify_runner=lambda root, **_: "tests executed",
        check_runner=lambda root: "checked",
        **kwargs,
    )


@pytest.mark.parametrize("failed_checkpoint", [False, True], ids=["first-verify", "resume-failed-verify"])
def test_external_import_is_repaired_in_manifest_before_verify(
    tmp_path, offline_install, failed_checkpoint,
):
    root = _project(tmp_path / "project")
    _checkpoint(root, failed=failed_checkpoint)
    command, calls = _fake_ai(tmp_path, ["sample-distribution"])

    result = _pipeline(command).run(root, resume=True)

    assert result.status == "success", format_greenfield_result(result, "text")
    assert "sample_lib" in json.loads(calls.read_text().strip())
    assert (root / "pyproject.toml").read_text().replace(
        'dependencies = ["sample-distribution"]\n', ""
    ) == _BASELINE_MANIFEST
    assert [deps for _, deps in offline_install] == [[], ["sample-distribution"]]
    assert all(argv[-2:] == ["-e", "."] and "sample-distribution" not in argv
               for argv, _ in offline_install)
    state = json.loads((root / ".codd/verify/exec_env.json").read_text())
    venv_bin = Path(state["path_prepend_dirs"][0])
    interpreter = venv_bin / ("python.exe" if os.name == "nt" else "python")
    subprocess.run([str(interpreter), "-I", "-c", "from demo import VALUE; assert VALUE == 42"], check=True)


def test_failed_toolchain_explains_the_fix_and_how_to_resume(tmp_path, monkeypatch, offline_install):
    root = _project(tmp_path / "project", external=False)
    _checkpoint(root)
    command, calls = _fake_ai(tmp_path, ["sample-distribution"])
    real_run = subprocess.run
    venv_available = False

    def run(argv, *args, **kwargs):
        if not venv_available and isinstance(argv, list) and argv[1:3] == ["-m", "venv"]:
            return subprocess.CompletedProcess(argv, 1, "", "ensurepip is unavailable")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(project_types.subprocess, "run", run)
    logs = []
    pipeline = _pipeline(command, echo=logs.append)
    result = pipeline.run(root, resume=True)

    assert result.status == "failed"
    assert "ensurepip is unavailable" in result.error
    guidance = (
        "Check venv support in the interpreter running CoDD and write access to .venv. "
        "After correcting the issue, use the resume command to retry provisioning."
    )
    resume = "codd greenfield --resume"
    report = format_greenfield_result(result, "text").splitlines()
    assert f"Inspect: {guidance}" in report
    assert f"Resume:  {resume}" in report
    assert f"[greenfield] inspect: {guidance}" in logs
    assert f"[greenfield] resume:  {resume}" in logs
    assert result.to_dict()["failure"]["inspect_command"] == guidance
    assert result.to_dict()["failure"]["resume_command"] == resume
    assert not calls.exists()

    venv_available = True
    resumed = pipeline.run(root, resume=True)
    assert resumed.status == "success", format_greenfield_result(resumed, "text")
    assert [deps for _, deps in offline_install] == [[]]
    assert not calls.exists()


@pytest.mark.parametrize("proposal", [[], ["unrelated-distribution"]], ids=["empty", "wrong"])
def test_unresolved_import_stays_red_and_reporting_is_bounded(tmp_path, offline_install, proposal):
    root = _project(tmp_path / "project")
    _checkpoint(root, failed=True)
    command, calls = _fake_ai(tmp_path, proposal)

    result = _pipeline(command).run(root, resume=True)

    assert result.status == "failed"
    assert "sample_lib" in result.error
    assert "not a code defect" not in result.error
    report = format_greenfield_result(result, "text")
    assert "Inspect: Declare the distributions providing these imports in pyproject.toml" in report
    assert "Resume:  codd greenfield --resume" in report
    assert len(calls.read_text().splitlines()) == 1
    assert (root / "pyproject.toml").read_text() == _BASELINE_MANIFEST
    assert not (root / ".codd/verify/exec_env.json").exists()


@pytest.mark.parametrize("has_state", [True, False], ids=["with-digest", "without-digest"])
def test_lazy_import_cannot_pass_from_a_stale_venv_without_its_declaration(
    tmp_path, offline_install, has_state,
):
    root = _project(tmp_path / "project", external=False)
    lazy_module = root / "src/demo/lazy.py"
    imports_external = "def read_value():\n    from sample_lib import VALUE\n    return VALUE\n"
    lazy_module.write_text(imports_external)
    manifest = root / "pyproject.toml"
    manifest.write_text(_BASELINE_MANIFEST.replace(
        'version = "0.0.0"', 'version = "0.0.0"\ndependencies = ["sample-distribution"]'
    ))
    _checkpoint(root)
    command, calls = _fake_ai(tmp_path, [])
    pipeline = _pipeline(command)

    assert pipeline.run(root, resume=True).status == "success"
    assert not calls.exists()

    manifest.write_text(_BASELINE_MANIFEST)
    state = root / ".codd/verify/exec_env.json"
    if not has_state:
        state.unlink()
        # A first-party-only probe can reuse the old venv. It must not make
        # the stale dependency metadata authoritative for later imports.
        sibling = root / "src/sibling.py"
        sibling.write_text("VALUE = 42\n")
        lazy_module.write_text("from sibling import VALUE\n")
        assert project_types.provision_project_env(root, language="python", project_name="demo").ok
        sibling.unlink()
        lazy_module.write_text(imports_external)
    _checkpoint(root, failed=True)
    result = pipeline.run(root, resume=True)

    assert result.status == "failed"
    assert "sample_lib (src/demo/lazy.py)" in result.error
    assert [deps for _, deps in offline_install] == [["sample-distribution"], []]
    assert not state.exists()


def test_dependency_free_manifest_remains_byte_identical(tmp_path, offline_install):
    root = _project(tmp_path / "project", external=False)
    _checkpoint(root)
    command, calls = _fake_ai(tmp_path, ["sample-distribution"])

    result = _pipeline(command).run(root, resume=True)

    assert result.status == "success", format_greenfield_result(result, "text")
    assert not calls.exists()
    assert (root / "pyproject.toml").read_text() == _BASELINE_MANIFEST
    assert [deps for _, deps in offline_install] == [[]]


@pytest.mark.parametrize(
    ("source", "module"),
    [("sibling/__init__.py", "sibling"), ("sibling.py", "sibling"), ("sibling/util.py", "sibling.util")],
    ids=["package", "module", "namespace-package"],
)
def test_project_owned_imports_provision_without_ai(tmp_path, offline_install, source, module):
    root = _project(tmp_path / "project", external=False)
    sibling = root / "src" / source
    sibling.parent.mkdir(exist_ok=True)
    sibling.write_text("VALUE = 42\n")
    (root / "src/demo/__init__.py").write_text(f"from {module} import VALUE\n")
    (root / "pyproject.toml").write_text(_BASELINE_MANIFEST)
    prompts = []

    def propose(prompt: str) -> str:
        prompts.append(prompt)
        return "[]"

    def provision():
        return project_types.provision_project_env(
            root, language="python", project_name="demo", propose_dependencies=propose,
        )

    result = provision()

    assert result.ok, result.detail
    assert not prompts
    assert (root / "pyproject.toml").read_text() == _BASELINE_MANIFEST
    assert [deps for _, deps in offline_install] == [[]]

    # A legacy usable venv without a manifest digest should still be reused.
    (root / ".codd/verify/exec_env.json").unlink()
    resumed = provision()
    assert resumed.ok, resumed.detail
    assert resumed.action == "up_to_date"
    assert not prompts
    assert [deps for _, deps in offline_install] == [[]]


def test_generated_ci_installs_runner_and_manifest_before_running_tests(tmp_path):
    root = _project(tmp_path / "project")
    (root / "pyproject.toml").write_text(_BASELINE_MANIFEST)

    _default_ci_scaffold_runner(root)

    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    commands = [step["run"] for step in workflow["jobs"]["test"]["steps"] if "run" in step]
    install_commands = [shlex.split(part) for command in commands[:-1] for part in command.split("&&")]
    assert commands[-1] == "pytest --tb=short -q"
    assert any(cmd[-2:] == ["-e", "."] for cmd in install_commands)
    assert any(any(arg.startswith("pytest") for arg in cmd) for cmd in install_commands)


@pytest.mark.parametrize(
    "proposal",
    [
        ["https://example.invalid/sample-distribution.tar.gz"],
        ["sample-distribution @ https://example.invalid/sample.whl"],
        ["../sample-distribution"],
        ["-e git+https://example.invalid/sample.git"],
        ["sample-distribution; python_version >= '3'"],
        [42],
    ],
    ids=["url", "direct-reference", "local-path", "pip-option", "marker", "non-string"],
)
def test_proposals_outside_plain_registry_requirements_never_reach_the_manifest_or_installer(
    tmp_path, offline_install, proposal,
):
    root = _project(tmp_path / "project")
    _checkpoint(root)
    command, calls = _fake_ai(tmp_path, proposal)

    result = _pipeline(command).run(root, resume=True)

    assert result.status == "failed"
    assert "registry requirement" in result.error
    assert len(calls.read_text().splitlines()) == 1
    assert (root / "pyproject.toml").read_text() == _BASELINE_MANIFEST
    assert [deps for _, deps in offline_install] == [[]]


def test_guarded_imports_need_no_declaration_and_no_ai(tmp_path, offline_install):
    root = _project(tmp_path / "project", external=False)
    (root / "src" / "demo" / "__init__.py").write_text(
        "from __future__ import annotations\n"
        "import typing\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import typing_only_lib\n"
        "if typing.TYPE_CHECKING:\n"
        "    from other_typing_only_lib import Shape\n"
        "try:\n"
        "    import optional_lib\n"
        "except ImportError:\n"
        "    optional_lib = None\n"
        "try:\n"
        "    from other_optional_lib import fast_path\n"
        "except ModuleNotFoundError:\n"
        "    fast_path = None\n"
    )
    _checkpoint(root)
    command, calls = _fake_ai(tmp_path, ["sample-distribution"])

    result = _pipeline(command).run(root, resume=True)

    assert result.status == "success", format_greenfield_result(result, "text")
    assert not calls.exists()
    assert (root / "pyproject.toml").read_text() == _BASELINE_MANIFEST
    assert [deps for _, deps in offline_install] == [[]]


def test_dependency_repair_prompt_carries_the_project_requirements(tmp_path, offline_install):
    root = _project(tmp_path / "project")
    requirement = "The only runtime dependency is the sample distribution."
    (root / "requirements.md").write_text(f"# Requirements\n\n{requirement}\n")
    _checkpoint(root)
    command, calls = _fake_ai(tmp_path, ["sample-distribution"])

    result = _pipeline(command).run(root, resume=True)

    assert result.status == "success", format_greenfield_result(result, "text")
    prompt = json.loads(calls.read_text().strip())
    assert requirement in prompt
    assert "sample_lib" in prompt
