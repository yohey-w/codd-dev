"""Tests for --feedback flag across generate, restore, and propagate."""

import subprocess
from pathlib import Path

import pytest
import yaml

import codd.generator as generator_module
from codd.cli import main
from codd.generator import (
    WaveArtifact,
    _build_generation_prompt,
)
from codd.propagator import (
    AffectedDoc,
    _build_update_prompt,
)
from codd.restore import _build_restoration_prompt


# -- Fixtures ----------------------------------------------------------------


FEEDBACK_TEXT = (
    "CRITICAL: Missing authentication flow details. "
    "The document does not describe how JWT tokens are validated or refreshed. "
    "WARNING: No error handling section. Add failure modes for each API endpoint."
)


def _make_artifact(node_id="design:system-design", output="docs/design/system_design.md", title="System Design"):
    return WaveArtifact(
        wave=2,
        node_id=node_id,
        output=output,
        title=title,
        depends_on=[],
        conventions=[],
        modules=["auth", "tasks"],
    )


# -- generate: _build_generation_prompt with feedback -------------------------


def test_generation_prompt_without_feedback():
    artifact = _make_artifact()
    prompt = _build_generation_prompt(artifact, [], [])
    assert "REVIEW FEEDBACK" not in prompt


def test_generation_prompt_with_feedback():
    artifact = _make_artifact()
    prompt = _build_generation_prompt(artifact, [], [], feedback=FEEDBACK_TEXT)
    assert "REVIEW FEEDBACK" in prompt
    assert "previous generation attempt" in prompt
    assert "JWT tokens" in prompt
    assert "MUST address ALL" in prompt


def test_generation_prompt_feedback_before_final_instruction():
    artifact = _make_artifact()
    prompt = _build_generation_prompt(artifact, [], [], feedback=FEEDBACK_TEXT)
    feedback_pos = prompt.index("REVIEW FEEDBACK")
    final_pos = prompt.index("Final instruction")
    assert feedback_pos < final_pos


# -- restore: _build_restoration_prompt with feedback -------------------------


def test_restoration_prompt_without_feedback():
    artifact = _make_artifact()
    prompt = _build_restoration_prompt(artifact, [])
    assert "REVIEW FEEDBACK" not in prompt


def test_restoration_prompt_with_feedback():
    artifact = _make_artifact()
    prompt = _build_restoration_prompt(artifact, [], feedback=FEEDBACK_TEXT)
    assert "REVIEW FEEDBACK" in prompt
    assert "previous restoration attempt" in prompt
    assert "JWT tokens" in prompt


def test_restoration_prompt_requirement_with_feedback():
    artifact = _make_artifact(
        node_id="req:requirements",
        output="docs/requirements/requirements.md",
        title="Requirements",
    )
    prompt = _build_restoration_prompt(artifact, [], feedback=FEEDBACK_TEXT)
    assert "REVIEW FEEDBACK" in prompt
    assert "INFERRING REQUIREMENTS" in prompt  # requirement-specific header preserved


# -- propagate: _build_update_prompt with feedback ----------------------------


def test_update_prompt_without_feedback():
    doc = AffectedDoc(
        node_id="design:system-design",
        path="docs/design/system_design.md",
        title="System Design",
        modules=["auth"],
        matched_modules=["auth"],
        changed_files=["src/auth/service.py"],
    )
    prompt = _build_update_prompt(doc, "# Content\n\nBody.", "diff text")
    assert "REVIEW FEEDBACK" not in prompt


def test_update_prompt_with_feedback():
    doc = AffectedDoc(
        node_id="design:system-design",
        path="docs/design/system_design.md",
        title="System Design",
        modules=["auth"],
        matched_modules=["auth"],
        changed_files=["src/auth/service.py"],
    )
    prompt = _build_update_prompt(doc, "# Content\n\nBody.", "diff text", feedback=FEEDBACK_TEXT)
    assert "REVIEW FEEDBACK" in prompt
    assert "previous update attempt" in prompt
    assert "JWT tokens" in prompt


# -- CLI integration: --feedback flag -----------------------------------------


BASE_CONFIG = {
    "version": "0.1.0",
    "project": {"name": "test", "language": "python"},
    "ai_command": "mock-ai --print",
    "scan": {
        "source_dirs": ["src"],
        "doc_dirs": ["docs/design/", "docs/requirements/"],
        "test_dirs": [],
        "config_files": [],
        "exclude": [],
    },
    "graph": {"store": "jsonl", "path": "codd/scan"},
    "wave_config": {
        "2": [
            {
                "node_id": "design:system-design",
                "output": "docs/design/system_design.md",
                "title": "System Design",
                "modules": ["auth"],
                "depends_on": [],
                "conventions": [],
            },
        ],
    },
}


def _setup_cli_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(BASE_CONFIG, sort_keys=False, allow_unicode=True),
    )
    return project


def test_generate_cli_feedback_flag(tmp_path, monkeypatch):
    from click.testing import CliRunner

    project = _setup_cli_project(tmp_path)
    prompts: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        prompts.append(input)
        return subprocess.CompletedProcess(
            args=command, returncode=0,
            stdout="# System Design\n\n## 1. Overview\n\nContent.\n\n## 2. Architecture\n\nArch.\n\n## 3. Open Questions\n\nNone.\n",
            stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, [
        "generate", "--wave", "2", "--path", str(project),
        "--feedback", FEEDBACK_TEXT,
    ])

    assert result.exit_code == 0
    assert len(prompts) == 1
    assert "REVIEW FEEDBACK" in prompts[0]
    assert "JWT tokens" in prompts[0]


def test_propagate_cli_feedback_flag(tmp_path, monkeypatch):
    from click.testing import CliRunner

    project = _setup_cli_project(tmp_path)
    # Create a design doc to update
    design_dir = project / "docs" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "system_design.md").write_text(
        "---\ncodd:\n  node_id: design:system-design\n  modules: [auth]\n---\n\n# System Design\n\n## Overview\n\nContent.\n"
    )
    prompts: list[str] = []

    def fake_run(command, *, capture_output=False, text=False,
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
        prompts.append(input)
        return subprocess.CompletedProcess(
            args=command, returncode=0, stderr="",
            stdout="## 1. Overview\n\nUpdated.\n",
        )

    monkeypatch.setattr("codd.propagator.subprocess.run", fake_run)
    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, [
        "propagate", "--path", str(project), "--update",
        "--feedback", FEEDBACK_TEXT,
    ])

    assert result.exit_code == 0
    assert len(prompts) >= 1
    assert "REVIEW FEEDBACK" in prompts[0]
