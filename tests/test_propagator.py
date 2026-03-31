"""Tests for codd propagate — reverse propagation from code to design docs."""

import subprocess
from pathlib import Path

import pytest
import yaml

import codd.generator as generator_module
from codd.cli import main
from codd.propagator import (
    AffectedDoc,
    _build_update_prompt,
    _find_design_docs_by_modules,
    _map_files_to_modules,
    run_propagate,
)


# -- Fixtures ----------------------------------------------------------------


BASE_CONFIG = {
    "version": "0.1.0",
    "project": {"name": "taskboard", "language": "python"},
    "ai_command": "mock-ai --print",
    "scan": {
        "source_dirs": ["src"],
        "test_dirs": ["tests"],
        "doc_dirs": ["docs/design/", "docs/requirements/", "docs/detailed_design/"],
        "config_files": [],
        "exclude": [],
    },
    "graph": {"store": "jsonl", "path": "codd/scan"},
    "bands": {"green": {"min_confidence": 0.90, "min_evidence_count": 2}},
    "wave_config": {
        "1": [
            {
                "node_id": "req:taskboard-requirements",
                "output": "docs/requirements/requirements.md",
                "title": "TaskBoard Requirements",
                "modules": ["auth", "tasks", "notifications"],
                "depends_on": [],
                "conventions": [],
            },
        ],
        "2": [
            {
                "node_id": "design:system-design",
                "output": "docs/design/system_design.md",
                "title": "TaskBoard System Design",
                "modules": ["auth", "tasks", "notifications"],
                "depends_on": [],
                "conventions": [],
            },
        ],
        "3": [
            {
                "node_id": "design:auth-detail",
                "output": "docs/detailed_design/auth_detail.md",
                "title": "Auth Module Detailed Design",
                "modules": ["auth"],
                "depends_on": [],
                "conventions": [],
            },
        ],
    },
}


def _setup_project(tmp_path: Path) -> Path:
    """Create a project with config, source files, and design docs."""
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(BASE_CONFIG, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # Source files
    (project / "src" / "auth").mkdir(parents=True)
    (project / "src" / "auth" / "service.py").write_text("class AuthService:\n    pass\n")
    (project / "src" / "tasks").mkdir(parents=True)
    (project / "src" / "tasks" / "service.py").write_text("class TaskService:\n    pass\n")

    # Design docs with modules frontmatter
    _write_design_doc(
        project / "docs" / "design" / "system_design.md",
        node_id="design:system-design",
        title="TaskBoard System Design",
        modules=["auth", "tasks", "notifications"],
        body="## 1. Overview\n\nSystem overview.\n\n## 2. Architecture\n\nArch details.\n",
    )
    _write_design_doc(
        project / "docs" / "detailed_design" / "auth_detail.md",
        node_id="design:auth-detail",
        title="Auth Module Detailed Design",
        modules=["auth"],
        body="## 1. Overview\n\nAuth detail.\n",
    )
    _write_design_doc(
        project / "docs" / "requirements" / "requirements.md",
        node_id="req:taskboard-requirements",
        title="TaskBoard Requirements",
        modules=["auth", "tasks", "notifications"],
        body="## 1. Overview\n\nRequirements.\n",
    )

    return project


def _write_design_doc(
    path: Path,
    *,
    node_id: str,
    title: str,
    modules: list[str],
    body: str,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    codd_meta = {
        "node_id": node_id,
        "type": "design",
        "title": title,
        "modules": modules,
    }
    frontmatter = yaml.safe_dump({"codd": codd_meta}, sort_keys=False)
    path.write_text(f"---\n{frontmatter}---\n\n# {title}\n\n{body}", encoding="utf-8")


@pytest.fixture
def mock_propagate_ai(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check):
        calls.append({"command": command, "input": input})
        # Return updated body
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                "## 1. Overview\n\n"
                "Updated system overview reflecting code changes.\n\n"
                "## 2. Architecture\n\n"
                "Updated arch details.\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)
    return calls


# -- Unit tests: _map_files_to_modules --------------------------------------


def test_map_files_to_modules_basic():
    files = ["src/auth/service.py", "src/tasks/models.py", "README.md"]
    result = _map_files_to_modules(files, ["src"])
    assert result == {
        "src/auth/service.py": "auth",
        "src/tasks/models.py": "tasks",
    }
    # README not in any source dir → excluded
    assert "README.md" not in result


def test_map_files_to_modules_nested_source_dir():
    files = ["packages/core/auth/handler.ts"]
    result = _map_files_to_modules(files, ["packages/core"])
    assert result == {"packages/core/auth/handler.ts": "auth"}


def test_map_files_to_modules_root_level_file_excluded():
    """Files directly in source dir (no module subdir) are excluded."""
    files = ["src/main.py"]
    result = _map_files_to_modules(files, ["src"])
    assert result == {}


def test_map_files_to_modules_multiple_source_dirs():
    files = ["src/auth/a.py", "lib/utils/b.py"]
    result = _map_files_to_modules(files, ["src", "lib"])
    assert result == {"src/auth/a.py": "auth", "lib/utils/b.py": "utils"}


# -- Unit tests: _find_design_docs_by_modules --------------------------------


def test_find_design_docs_by_modules(tmp_path):
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())

    docs = _find_design_docs_by_modules(
        project, config, {"auth"}, {"src/auth/service.py": "auth"},
    )

    node_ids = {d.node_id for d in docs}
    # system-design covers auth, auth-detail covers auth, requirements covers auth
    assert "design:system-design" in node_ids
    assert "design:auth-detail" in node_ids
    assert "req:taskboard-requirements" in node_ids


def test_find_design_docs_excludes_unrelated_modules(tmp_path):
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())

    docs = _find_design_docs_by_modules(
        project, config, {"notifications"}, {"src/notifications/service.py": "notifications"},
    )

    node_ids = {d.node_id for d in docs}
    # auth-detail only covers ["auth"] → should NOT be found
    assert "design:auth-detail" not in node_ids
    # system-design covers notifications
    assert "design:system-design" in node_ids


def test_find_design_docs_returns_empty_for_unknown_module(tmp_path):
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())

    docs = _find_design_docs_by_modules(
        project, config, {"billing"}, {"src/billing/service.py": "billing"},
    )
    assert docs == []


# -- Unit tests: _build_update_prompt ----------------------------------------


def test_build_update_prompt_contains_key_elements():
    doc = AffectedDoc(
        node_id="design:system-design",
        path="docs/design/system_design.md",
        title="System Design",
        modules=["auth", "tasks"],
        matched_modules=["auth"],
        changed_files=["src/auth/service.py"],
    )
    current = "---\ncodd:\n  node_id: design:system-design\n---\n\n# System Design\n\n## Overview\n\nOld content.\n"
    diff = "diff --git a/src/auth/service.py\n+    def new_method(self):\n"

    prompt = _build_update_prompt(doc, current, diff)

    assert "UPDATING" in prompt
    assert "design:system-design" in prompt
    assert "auth" in prompt
    assert "src/auth/service.py" in prompt
    assert "Old content" in prompt  # current doc included
    assert "new_method" in prompt  # code diff included
    assert "bug fix" in prompt.lower()  # mentions not updating for bug fixes
    assert "UNCHANGED" in prompt  # mentions leaving body unchanged


# -- Integration test: run_propagate with mocked git -------------------------


def test_run_propagate_analysis_only(tmp_path, monkeypatch):
    """run_propagate without --update returns affected docs without calling AI."""
    project = _setup_project(tmp_path)

    monkeypatch.setattr(
        "codd.propagator.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0,
            stdout="src/auth/service.py\nsrc/auth/models.py\n", stderr="",
        ),
    )

    result = run_propagate(project, diff_target="HEAD", update=False)

    assert len(result.changed_files) == 2
    assert result.file_module_map == {
        "src/auth/service.py": "auth",
        "src/auth/models.py": "auth",
    }
    assert len(result.affected_docs) > 0
    affected_ids = {d.node_id for d in result.affected_docs}
    assert "design:auth-detail" in affected_ids
    assert "design:system-design" in affected_ids
    assert result.updated == []  # no update without flag


def test_run_propagate_with_update(tmp_path, monkeypatch):
    """run_propagate with update=True calls AI and updates docs."""
    project = _setup_project(tmp_path)
    ai_calls: list[dict] = []

    # Mock both git (propagator.subprocess) and AI (generator.subprocess)
    def patched_subprocess(command, *, capture_output=False, text=False,
                           cwd=None, check=False, input=None, **kw):
        if command[0] == "git" and "diff" in command:
            if "--name-only" in command:
                return subprocess.CompletedProcess(
                    args=command, returncode=0,
                    stdout="src/auth/service.py\n", stderr="",
                )
            return subprocess.CompletedProcess(
                args=command, returncode=0,
                stdout="diff --git a/src/auth/service.py\n+new code\n", stderr="",
            )
        # AI call
        ai_calls.append({"command": command, "input": input})
        return subprocess.CompletedProcess(
            args=command, returncode=0, stderr="",
            stdout=(
                "## 1. Overview\n\nUpdated overview.\n\n"
                "## 2. Architecture\n\nUpdated arch.\n"
            ),
        )

    monkeypatch.setattr("codd.propagator.subprocess.run", patched_subprocess)
    monkeypatch.setattr(generator_module.subprocess, "run", patched_subprocess)

    result = run_propagate(project, diff_target="HEAD", update=True)

    assert len(result.updated) > 0
    assert len(ai_calls) > 0  # AI was called

    # Verify prompt contains doc content and diff
    prompt = ai_calls[0]["input"]
    assert "UPDATING" in prompt
    assert "CODE DIFF" in prompt


# -- CLI test ----------------------------------------------------------------


def test_propagate_cli_analysis_mode(tmp_path, monkeypatch):
    """CLI 'codd propagate' shows analysis without updating."""
    from click.testing import CliRunner

    project = _setup_project(tmp_path)

    monkeypatch.setattr(
        "codd.propagator.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0,
            stdout="src/auth/service.py\n", stderr="",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["propagate", "--path", str(project)])

    assert result.exit_code == 0
    assert "auth" in result.output
    assert "design:auth-detail" in result.output
    assert "needs review" in result.output
    assert "--update" in result.output  # suggests running with --update


def test_propagate_cli_no_changes(tmp_path, monkeypatch):
    """CLI shows message when no files changed."""
    from click.testing import CliRunner

    project = _setup_project(tmp_path)

    monkeypatch.setattr(
        "codd.propagator.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr="",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["propagate", "--path", str(project)])

    assert result.exit_code == 0
    assert "No changed files" in result.output
