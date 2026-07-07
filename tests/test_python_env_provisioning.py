"""Python test-environment provisioning + exec-path projection (v3.15.0).

Closes the two root causes that made ``codd greenfield --language python``
un-runnable unattended on a python3-only host:

* 根因1 — the profile verify argv is a bare ``python`` (absent when only
  ``python3`` exists);
* 根因2 — a generated CLI/subprocess-e2e helper spawns ``[sys.executable, "-c",
  ...]`` assuming an INSTALLED package, but the pipeline never provisioned a venv
  nor ran ``pip install -e .`` (in-process pytest sees ``src`` via ``pythonpath``,
  but that never propagates to a subprocess child).

The fix provisions a project-local ``.venv`` (harness-owned pinned pytest +
``pip install -e .``), records a harness-owned state artifact, and the verify
runner prepends the venv bin dir to the spawn PATH so an UNCHANGED ``python``/
``pytest`` argv resolves to the venv interpreter — whose ``sys.executable`` then
resolves the editable-installed package in the e2e subprocess child.

These are heavier integration tests: they create a real venv and ``pip install``
the pinned pytest + the fixture package ONCE (module-scoped fixture), so they need
the same network the greenfield dogfood needs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from codd.dag import DAG, Node
from codd.repair.verify_runner import VerifyRunner


def _write_min_python_project(root: Path, *, project_name: str) -> str:
    """Scaffold a minimal src-layout python project + an e2e-shaped test.

    Returns the resolved canonical package name. Uses the REAL harness scaffolder
    (``resolve_layout_profile`` + ``scaffold_layout``) so the pyproject/packaging is
    exactly what the greenfield pipeline produces, then overwrites the package body
    with importable symbols and adds an e2e test that spawns ``sys.executable``.
    """
    from codd.project_types import resolve_layout_profile, scaffold_layout

    config = {"project": {"language": "python", "name": project_name}}
    profile = resolve_layout_profile(
        language="python",
        project_name=project_name,
        source_dirs=None,
        test_dirs=None,
        config=config,
        project_root=root,
    )
    assert profile is not None
    scaffold_layout(root, profile)
    pkg = profile.package_name
    pkg_dir = root / "src" / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    (pkg_dir / "core.py").write_text("CORE_VALUE = 7\n", encoding="utf-8")
    # A codd.yaml so the verify runner resolves the python language contract.
    (root / "codd.yaml").write_text(
        f"project:\n  language: python\n  name: {project_name}\n", encoding="utf-8"
    )
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    # e2e-shaped: spawn a fresh interpreter (cwd = an unrelated tmp dir) and import
    # the package. This ONLY passes when sys.executable is an interpreter that has
    # the package INSTALLED — the exact 根因2 propagation the venv provision fixes.
    (tests_dir / "test_e2e_subprocess.py").write_text(
        textwrap.dedent(
            f"""
            import subprocess
            import sys
            import tempfile


            def test_package_importable_from_subprocess_child():
                with tempfile.TemporaryDirectory() as d:
                    result = subprocess.run(
                        [sys.executable, "-c",
                         "import {pkg}; assert {pkg}.VALUE == 42"],
                        cwd=d,
                        capture_output=True,
                        text=True,
                    )
                    assert result.returncode == 0, result.stderr
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return pkg


def _python3_only_path(tmp: Path) -> str:
    """A PATH that exposes ``python3`` but neither ``python`` nor ``pytest``.

    Simulates the real python3-only host regardless of the CI box's ambient PATH:
    a single dir containing only a ``python3`` symlink to the running interpreter.
    """
    real_python3 = shutil.which("python3") or sys.executable
    bindir = tmp / "py3only"
    bindir.mkdir(exist_ok=True)
    link = bindir / "python3"
    if not link.exists():
        os.symlink(real_python3, link)
    return str(bindir)


@pytest.fixture(scope="module")
def provisioned_project(tmp_path_factory):
    """Scaffold + PROVISION a python project once (real venv + pip install).

    Yields ``(project_root, venv_bin_dir, package_name)``. The provision is the
    single network-touching step, shared across the tests below.
    """
    from codd.project_types import provision_project_env

    root = tmp_path_factory.mktemp("provisioned_py")
    project_name = "red2pkg"
    pkg = _write_min_python_project(root, project_name=project_name)
    config = {"project": {"language": "python", "name": project_name}}
    result = provision_project_env(
        root,
        language="python",
        project_name=project_name,
        source_dirs=None,
        test_dirs=None,
        config=config,
    )
    assert result.ok, f"provision failed: {result.detail}"
    venv_bin = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    assert venv_bin.is_dir()
    yield root, venv_bin, pkg


def _settings(project_name: str) -> dict:
    return {"project": {"language": "python", "name": project_name}}


# ── RED-2: the whole flow, verified under a python3-only PATH ──────────────────


def test_greenfield_python_verify_is_green_under_python3_only_path(
    provisioned_project, tmp_path, monkeypatch
):
    """RED-2 (green after fix): with the venv provisioned + its bin dir recorded in
    the state artifact, the verify runner prepends it to PATH so the bare
    ``python``/``pytest`` argv resolves to the venv, whose sys.executable resolves
    the editable-installed package in the e2e subprocess child → GREEN — on a host
    exposing only ``python3``."""
    root, _venv_bin, pkg = provisioned_project
    monkeypatch.setenv("PATH", _python3_only_path(tmp_path))
    settings = _settings("red2pkg")
    runner = VerifyRunner(root, settings)
    executed, command, summary, failure = runner._run_test_command(settings)
    assert executed is True
    assert failure is None, f"expected green, got summary={summary!r} failure={failure}"


def test_without_state_artifact_python3_only_path_is_red(tmp_path, monkeypatch):
    """Byte-identity / root-cause documentation: with NO env-provision state
    artifact (a brownfield / un-provisioned run), the verify command's bare
    ``python``/``pytest`` argv is unresolvable on a python3-only host → RED. No venv
    is grown; the runner behaves exactly as today."""
    root = tmp_path / "unprovisioned"
    root.mkdir()
    _write_min_python_project(root, project_name="red2pkg")
    # No .codd/verify/exec_env.json here → no PATH prepend.
    monkeypatch.setenv("PATH", _python3_only_path(tmp_path))
    settings = _settings("red2pkg")
    runner = VerifyRunner(root, settings)
    _executed, _command, _summary, failure = runner._run_test_command(settings)
    assert failure is not None  # not green (TOOL_MISSING / command-not-found)


# ── anti-false-green (DoD 4): isolation + bare-basename stay RED ───────────────


def test_provisioned_venv_is_isolated_from_codd_environment(provisioned_project):
    """(a) A package present in the CoDD environment but NOT declared by the project
    must FAIL to import in the venv — proving the venv is isolated (the false-green
    the PYTHONPATH-injection alternative would have introduced is structurally
    absent). (b) The project's own package imports fine (editable install works)."""
    root, venv_bin, pkg = provisioned_project
    venv_python = venv_bin / ("python.exe" if os.name == "nt" else "python")
    # (a) `yaml` (PyYAML) is imported by CoDD itself but is NOT a project dep.
    leaked = subprocess.run(
        [str(venv_python), "-c", "import yaml"], capture_output=True, text=True
    )
    assert leaked.returncode != 0, "CoDD's site-packages leaked into the project venv"
    # (b) the editable-installed project package resolves.
    own = subprocess.run(
        [str(venv_python), "-c", f"import {pkg}; assert {pkg}.VALUE == 42"],
        capture_output=True,
        text=True,
    )
    assert own.returncode == 0, own.stderr


def test_bare_basename_import_stays_red_in_venv(provisioned_project):
    """A source module resolves ONLY package-qualified (``<pkg>.core``), never by
    bare basename (``import core``) — the src-layout/editable-install anti-false-
    green invariant is preserved (no ``pythonpath="."`` shortcut smuggled in)."""
    root, venv_bin, pkg = provisioned_project
    venv_python = venv_bin / ("python.exe" if os.name == "nt" else "python")
    bare = subprocess.run(
        [str(venv_python), "-c", "import core"], capture_output=True, text=True
    )
    assert bare.returncode != 0
    qualified = subprocess.run(
        [str(venv_python), "-c", f"import {pkg}.core; assert {pkg}.core.CORE_VALUE == 7"],
        capture_output=True,
        text=True,
    )
    assert qualified.returncode == 0, qualified.stderr


# ── dispatch parity: python declares the env_provisioner; others NO-OP ─────────


def test_python_declares_registered_env_provisioner():
    from codd import project_types as pt
    from codd.languages.registry import default_registry

    block = dict(default_registry.resolve("python").extra["legacy_project_types"])
    assert block["env_provisioner"] in pt._ENV_PROVISIONERS_BY_REALIZER


def test_non_python_stacks_are_env_provision_noops(tmp_path):
    from codd.project_types import provision_project_env

    for language in ("typescript", "go"):
        result = provision_project_env(
            tmp_path,
            language=language,
            project_name="x",
            source_dirs=None,
            test_dirs=None,
            config={"project": {"language": language, "name": "x"}},
        )
        assert result.ok is True
        assert result.action == "unsupported"
        assert not (tmp_path / ".venv").exists()


def test_every_registered_env_provisioner_is_declared_by_some_profile():
    from codd import project_types as pt
    from codd.languages.registry import default_registry

    declared: set[str] = set()
    for profile in default_registry.all_profiles():
        block = profile.extra.get("legacy_project_types")
        if isinstance(block, dict) and block.get("env_provisioner"):
            declared.add(str(block["env_provisioner"]))
    assert set(pt._ENV_PROVISIONERS_BY_REALIZER) == declared


# ── v3.15.0 fold: THIRD spawn surface (template.execute) under python3-only ────


def test_verification_template_surface_resolves_via_state_artifact_python3_only(
    provisioned_project, tmp_path, monkeypatch
):
    """Faithful reproduction of the dogfood's 5 verification-node failures. A
    ``verification_test`` node runs ``python -m pytest`` through the pytest_http
    TEMPLATE — the THIRD verify spawn surface, distinct from the contract executor
    and the evidence command. With the venv provisioned + its bin recorded in the
    state artifact, the runner threads a PATH-prepended env into ``template.execute``
    so the bare ``python`` resolves to the venv interpreter → GREEN — on a host
    exposing only ``python3``. RED before this surface was covered (the exact
    ``/bin/sh: 1: python: not found`` the dogfood hit)."""
    root, _venv_bin, _pkg = provisioned_project
    e2e_dir = root / "tests" / "e2e"
    e2e_dir.mkdir(parents=True, exist_ok=True)
    (e2e_dir / "test_ping.py").write_text(
        "def test_ping():\n    assert True\n", encoding="utf-8"
    )
    monkeypatch.setenv("PATH", _python3_only_path(tmp_path))
    settings = _settings("red2pkg")
    runner = VerifyRunner(root, settings)
    node = Node(
        "verification:e2e:tests/e2e/test_ping.py",
        "verification_test",
        attributes={
            "kind": "e2e",
            "template_ref": "pytest_http",
            "source": "tests/e2e/test_ping.py",
        },
    )
    dag = DAG()
    dag.add_node(node)
    results = runner._run_verification_tests(dag, settings)
    assert len(results) == 1
    assert results[0]["passed"] is True, results[0].get("output")
