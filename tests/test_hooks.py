"""Tests for CoDD pre-commit hook integration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codd.cli import main


BASE_CONFIG = {
    "version": "0.1.0",
    "project": {"name": "hook-project", "language": "python"},
    "scan": {
        "source_dirs": [],
        "test_dirs": [],
        "doc_dirs": ["docs/"],
        "config_files": [],
        "exclude": [],
    },
    "graph": {"store": "jsonl", "path": "codd/scan"},
    "bands": {
        "green": {"min_confidence": 0.90, "min_evidence_count": 2},
        "amber": {"min_confidence": 0.50},
    },
    "propagation": {"max_depth": 10},
}


def _setup_git_project(tmp_path: Path, docs: dict[str, str]) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "codd").mkdir()
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(BASE_CONFIG, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)

    for relative_path, content in docs.items():
        file_path = project / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", relative_path], cwd=project, check=True)

    return project


def _make_codd_wrapper(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "codd"
    repo_root = Path(__file__).resolve().parent.parent
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f"export PYTHONPATH=\"{repo_root}:${{PYTHONPATH:-}}\"\n"
        f"exec {sys.executable} -m codd.cli \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return bin_dir


@pytest.mark.skipif(sys.platform == "win32", reason="bash hooks not supported on Windows")
def test_pre_commit_blocks_staged_markdown_without_frontmatter(tmp_path):
    project = _setup_git_project(
        tmp_path,
        {"docs/notes.md": "# Missing frontmatter\n"},
    )
    bin_dir = _make_codd_wrapper(tmp_path)
    hook_path = Path(__file__).resolve().parent.parent / "hooks" / "pre-commit"

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        [str(hook_path)],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 1
    assert "ERROR: docs/notes.md is missing CoDD YAML frontmatter" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="bash hooks not supported on Windows")
def test_pre_commit_allows_valid_staged_markdown(tmp_path):
    project = _setup_git_project(
        tmp_path,
        {
            "docs/requirements.md": """---
codd:
  node_id: "req:project-requirements"
  type: requirement
---

# Requirements
""",
        },
    )
    bin_dir = _make_codd_wrapper(tmp_path)
    hook_path = Path(__file__).resolve().parent.parent / "hooks" / "pre-commit"

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        [str(hook_path)],
        cwd=project,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert "OK: validated 1 Markdown files under configured doc_dirs" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks require elevated privileges on Windows")
def test_hooks_install_creates_pre_commit_symlink(tmp_path):
    project = _setup_git_project(
        tmp_path,
        {
            "docs/requirements.md": """---
codd:
  node_id: "req:project-requirements"
  type: requirement
---

# Requirements
""",
        },
    )
    runner = CliRunner()

    result = runner.invoke(main, ["hooks", "install", "--path", str(project)])

    assert result.exit_code == 0
    installed = project / ".git" / "hooks" / "pre-commit"
    assert installed.is_symlink()
    assert installed.resolve() == (Path(__file__).resolve().parent.parent / "hooks" / "pre-commit").resolve()
