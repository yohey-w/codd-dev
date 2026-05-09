from __future__ import annotations

from pathlib import Path
import re
import subprocess

from click.testing import CliRunner
import pytest
import yaml

import codd.implementer as implementer_module
from codd.cli import CoddCLIError, main
from codd.implementer import ImplementSpec, Implementer, implement_tasks


def _write_doc(
    project: Path,
    relative_path: str,
    *,
    node_id: str,
    body: str,
    depends_on: list[dict] | None = None,
    conventions: list[dict] | None = None,
) -> None:
    codd = {"node_id": node_id, "type": "design"}
    if depends_on is not None:
        codd["depends_on"] = depends_on
    if conventions is not None:
        codd["conventions"] = conventions
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"{yaml.safe_dump({'codd': codd}, sort_keys=False, allow_unicode=True)}"
        "---\n\n"
        f"{body.rstrip()}\n",
        encoding="utf-8",
    )


def _project(tmp_path: Path, *, language: str = "python") -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "codd").mkdir()
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": language},
                "ai_command": "mock-ai --print",
                "scan": {
                    "source_dirs": ["src/"],
                    "doc_dirs": ["docs/design/"],
                    "config_files": [],
                    "exclude": [],
                },
                "conventions": [{"targets": ["module:auth"], "reason": "Use explicit checks."}],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    _write_doc(
        project,
        "docs/design/auth.md",
        node_id="design:auth",
        body="# Auth Design\n\nBuild an auth service and tests.",
        depends_on=[{"id": "design:shared", "relation": "depends_on"}],
    )
    _write_doc(
        project,
        "docs/design/shared.md",
        node_id="design:shared",
        body="# Shared Design\n\nShared user model.",
    )
    return project


def _patch_ai(monkeypatch: pytest.MonkeyPatch, stdout: str | None = None) -> list[str]:
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        if stdout is not None:
            output = stdout
        else:
            match = re.search(r"Output paths: (?P<paths>[^\n]+)", input)
            first_output = match.group("paths").split(",")[0].strip() if match else "src/auth"
            output = (
                f"=== FILE: {first_output}/service.py ===\n"
                "```python\n"
                "def build_auth() -> bool:\n"
                "    return True\n"
                "```\n"
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


def test_implement_spec_basic() -> None:
    spec = ImplementSpec(design_node="docs/design/auth.md", output_paths=["src/auth"])

    assert spec.design_node == "docs/design/auth.md"
    assert spec.output_paths == ["src/auth"]
    assert spec.dependency_design_nodes == []


def test_implement_spec_multiple_outputs() -> None:
    spec = ImplementSpec("docs/design/auth.md", ["src/auth/", "tests/auth/"])

    assert spec.output_paths == ["src/auth/", "tests/auth/"]


def test_implement_spec_dependency_design_nodes() -> None:
    spec = ImplementSpec(
        "docs/design/auth.md",
        ["src/auth"],
        dependency_design_nodes=["docs/design/shared.md"],
    )

    assert spec.dependency_design_nodes == ["docs/design/shared.md"]


def test_implement_spec_rejects_empty_outputs() -> None:
    with pytest.raises(ValueError, match="output_paths"):
        ImplementSpec("docs/design/auth.md", [])


def test_run_implement_creates_output_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    _patch_ai(monkeypatch)
    spec = ImplementSpec("docs/design/auth.md", ["src/auth", "tests/auth"])

    Implementer(project).run_implement(spec)

    assert (project / "src" / "auth").is_dir()
    assert (project / "tests" / "auth").is_dir()


def test_run_implement_returns_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    _patch_ai(monkeypatch)
    spec = ImplementSpec("docs/design/auth.md", ["src/auth"])

    result = Implementer(project).run_implement(spec)

    assert result.design_node == "docs/design/auth.md"
    assert result.output_paths == [(project / "src" / "auth").resolve()]
    assert result.generated_files == [project / "src" / "auth" / "service.py"]
    assert result.error is None


def test_run_implement_writes_traceability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    _patch_ai(monkeypatch)

    Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    content = (project / "src" / "auth" / "service.py").read_text(encoding="utf-8")
    assert content.startswith("# @generated-by: codd implement")
    assert "# @generated-from: docs/design/auth.md (design:auth)" in content
    assert "# @design-node: docs/design/auth.md" in content


def test_run_implement_multiple_output_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    _patch_ai(
        monkeypatch,
        stdout=(
            "=== FILE: src/auth/service.py ===\n"
            "```python\n"
            "def service() -> bool:\n"
            "    return True\n"
            "```\n"
            "=== FILE: tests/auth/test_service.py ===\n"
            "```python\n"
            "def test_service():\n"
            "    assert True\n"
            "```\n"
        ),
    )

    result = Implementer(project).run_implement(
        ImplementSpec("docs/design/auth.md", ["src/auth", "tests/auth"])
    )

    assert project / "src" / "auth" / "service.py" in result.generated_files
    assert project / "tests" / "auth" / "test_service.py" in result.generated_files


def test_run_implement_includes_dependency_design_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    calls = _patch_ai(monkeypatch)

    Implementer(project).run_implement(
        ImplementSpec("docs/design/auth.md", ["src/auth"], dependency_design_nodes=["docs/design/shared.md"])
    )

    assert "docs/design/shared.md" in calls[0]
    assert "Shared user model" in calls[0]


def test_run_implement_rejects_file_outside_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    _patch_ai(
        monkeypatch,
        stdout=(
            "=== FILE: src/other/service.py ===\n"
            "```python\n"
            "def bad() -> bool:\n"
            "    return False\n"
            "```\n"
        ),
    )

    with pytest.raises(CoddCLIError, match="produced 0 generated files"):
        Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))


def test_cli_implement_design_output_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    _patch_ai(monkeypatch)

    result = CliRunner().invoke(
        main,
        [
            "implement",
            "--path",
            str(project),
            "--design",
            "docs/design/auth.md",
            "--output",
            "src/auth",
            "--output",
            "tests/auth",
            "--depends-on",
            "docs/design/shared.md",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Generated: src/auth/service.py (docs/design/auth.md)" in result.output


def test_implement_tasks_uses_configured_output_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    config_path = project / "codd" / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["implement"] = {"default_output_paths": {"docs/design/auth.md": ["src/configured"]}}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _patch_ai(monkeypatch)

    result = implement_tasks(project, design="docs/design/auth.md")

    assert result[0].generated_files == [project / "src" / "configured" / "service.py"]


def test_skip_generation_design_returns_empty_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    (project / "docs" / "design" / "auth.md").write_text(
        "---\ncodd:\n  node_id: design:auth\n  type: design\n---\n\nskip_generation: true\n",
        encoding="utf-8",
    )
    _patch_ai(monkeypatch, stdout="")

    result = Implementer(project).run_implement(ImplementSpec("docs/design/auth.md", ["src/auth"]))

    assert result.generated_files == []
