"""Tests for coherence-aware applier behavior in propagator and fixer."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml
from click.testing import CliRunner

import codd.generator as generator_module
from codd.cli import _load_coherence_context, main
from codd.coherence_engine import DriftEvent
from codd.fixer import (
    FailureInfo,
    _build_coherence_fix_context,
    _build_fix_prompt,
    run_fix,
)
from codd.propagator import AffectedDoc, _build_update_prompt, run_propagate


BASE_CONFIG = {
    "project": {"name": "demo", "language": "python"},
    "ai_command": "mock-ai --print",
    "scan": {
        "source_dirs": ["src"],
        "test_dirs": ["tests"],
        "doc_dirs": ["docs/design/"],
        "config_files": [],
        "exclude": [],
    },
    "graph": {"store": "jsonl", "path": "codd/scan"},
    "bands": {"green": {"min_confidence": 0.9, "min_evidence_count": 2}},
}


def _write_codd_config(project: Path, extra: dict | None = None) -> None:
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    config = dict(BASE_CONFIG)
    if extra:
        config.update(extra)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )


def _write_design_doc(project: Path) -> None:
    path = project / "docs" / "design" / "auth.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "codd:\n"
        "  node_id: design:auth\n"
        "  type: design\n"
        "  title: Auth Design\n"
        "  modules:\n"
        "    - auth\n"
        "---\n\n"
        "# Auth Design\n\n"
        "## Overview\n\n"
        "Old auth design.\n",
        encoding="utf-8",
    )


def _affected_doc() -> AffectedDoc:
    return AffectedDoc(
        node_id="design:auth",
        path="docs/design/auth.md",
        title="Auth Design",
        modules=["auth"],
        matched_modules=["auth"],
        changed_files=["src/auth/service.py"],
    )


def _event() -> DriftEvent:
    return DriftEvent(
        source_artifact="design_md",
        target_artifact="implementation",
        change_type="modified",
        payload={
            "description": "Primary color token drift",
            "file": "src/app.css",
            "token": "colors.Primary",
        },
        severity="amber",
        fix_strategy="hitl",
        kind="design_token_drift",
    )


def test_build_prompt_no_coherence_matches_existing_output():
    doc = _affected_doc()
    current = "# Auth Design\n\nOld content.\n"
    diff = "diff --git a/src/auth/service.py\n+new code\n"

    assert _build_update_prompt(doc, current, diff, coherence_context=None) == (
        _build_update_prompt(doc, current, diff)
    )


def test_build_prompt_with_lexicon_section():
    prompt = _build_update_prompt(
        _affected_doc(),
        "# Auth Design\n",
        "diff",
        coherence_context={"lexicon": "route_node: Stable route name"},
    )

    assert "## Project Lexicon (must respect naming conventions)" in prompt
    assert "route_node: Stable route name" in prompt


def test_build_prompt_with_design_md_section():
    prompt = _build_update_prompt(
        _affected_doc(),
        "# Auth Design\n",
        "diff",
        coherence_context={"design_md": "colors.Primary: '#1A73E8'"},
    )

    assert "## Design Tokens (must respect these values)" in prompt
    assert "colors.Primary" in prompt


def test_build_prompt_with_both_coherence_sections():
    prompt = _build_update_prompt(
        _affected_doc(),
        "# Auth Design\n",
        "diff",
        coherence_context={
            "lexicon": {"node_vocabulary": [{"id": "route_node"}]},
            "design_md": "spacing.Small: 8px",
        },
    )

    assert "Project Lexicon" in prompt
    assert "route_node" in prompt
    assert "Design Tokens" in prompt
    assert "spacing.Small" in prompt


def test_design_md_context_is_truncated():
    prompt = _build_update_prompt(
        _affected_doc(),
        "# Auth Design\n",
        "diff",
        coherence_context={"design_md": "x" * 2100},
    )

    section = prompt.split("## Design Tokens (must respect these values)", 1)[1]
    assert "x" * 2000 in section
    assert "x" * 2001 not in section


def test_propagate_cli_coherence_flag_is_in_help():
    result = CliRunner().invoke(main, ["propagate", "--help"])

    assert result.exit_code == 0
    assert "--coherence" in result.output


def test_load_coherence_context_from_configured_paths(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "config").mkdir()
    (project / "docs").mkdir()
    (project / "config" / "lexicon.yaml").write_text("lexicon: custom\n", encoding="utf-8")
    (project / "docs" / "DESIGN.md").write_text("# Tokens\n\ncolors.Primary\n", encoding="utf-8")
    _write_codd_config(
        project,
        {"lexicon_path": "config/lexicon.yaml", "design_md": "docs/DESIGN.md"},
    )

    context = _load_coherence_context(project)

    assert context["lexicon"] == "lexicon: custom\n"
    assert "colors.Primary" in context["design_md"]


def test_run_propagate_update_injects_coherence_context(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _write_codd_config(project)
    _write_design_doc(project)
    (project / "src" / "auth").mkdir(parents=True)
    (project / "src" / "auth" / "service.py").write_text("def login(): pass\n", encoding="utf-8")
    ai_prompts: list[str] = []

    def fake_subprocess(
        command,
        *,
        input=None,
        capture_output=False,
        text=False,
        cwd=None,
        check=False,
        **kwargs,
    ):
        if command[0] == "git" and "--name-only" in command:
            return subprocess.CompletedProcess(command, 0, stdout="src/auth/service.py\n", stderr="")
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, stdout="+def logout(): pass\n", stderr="")
        ai_prompts.append(input)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="# Auth Design\n\n## Overview\n\nUpdated auth design.\n",
            stderr="",
        )

    monkeypatch.setattr("codd.propagator.subprocess.run", fake_subprocess)
    monkeypatch.setattr(generator_module.subprocess, "run", fake_subprocess)

    result = run_propagate(
        project,
        update=True,
        coherence_context={"lexicon": "route_node", "design_md": "colors.Primary"},
    )

    assert result.updated == ["design:auth"]
    assert "Project Lexicon" in ai_prompts[0]
    assert "Design Tokens" in ai_prompts[0]


def test_build_coherence_fix_context_from_event():
    context = _build_coherence_fix_context(_event())

    assert "## Coherence Drift Event" in context
    assert "design_token_drift" in context
    assert "colors.Primary" in context


def test_build_fix_prompt_no_coherence_event_keeps_design_guard(tmp_path):
    prompt = _build_fix_prompt(
        tmp_path,
        [FailureInfo("local", "test", "failure", "trace", ["src/auth.py"])],
        "design context",
        {"project": {"name": "demo", "language": "python"}},
    )

    assert "Do NOT modify design documents." in prompt
    assert "Coherence Drift Event" not in prompt


def test_run_fix_with_coherence_event_allows_design_fix(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _write_codd_config(project)
    failure = FailureInfo("local", "test", "failure", "trace", ["src/auth.py"])
    prompts: list[str] = []

    monkeypatch.setattr("codd.fixer._detect_ci_failures", lambda project_root: [])
    monkeypatch.setattr("codd.fixer._build_fix_context", lambda *args, **kwargs: "design context")
    monkeypatch.setattr("codd.fixer._run_local_tests", lambda *args, **kwargs: [failure] if not prompts else [])

    def fake_invoke(ai_command: str, prompt: str, project_root: Path) -> str:
        prompts.append(prompt)
        return "## Diagnosis\n\nFixed.\n"

    monkeypatch.setattr("codd.fixer._invoke_fix_ai", fake_invoke)

    result = run_fix(project, push=False, coherence_event=_event())

    assert result.fixed is True
    assert "Coherence Drift Event" in prompts[0]
    assert "Design document changes are allowed" in prompts[0]
    assert "Do NOT modify design documents." not in prompts[0]


def test_fixer_backward_compat_no_coherence_event(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _write_codd_config(project)

    monkeypatch.setattr("codd.fixer._detect_ci_failures", lambda project_root: [])
    monkeypatch.setattr("codd.fixer._run_local_tests", lambda *args, **kwargs: [])

    result = run_fix(project, push=False)

    assert result.fixed is True
    assert result.attempts == []
