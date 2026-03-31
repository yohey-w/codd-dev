"""Tests for codd review — AI-powered artifact quality evaluation."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

import codd.generator as generator_module
from codd.cli import main
from codd.reviewer import (
    ReviewIssue,
    ReviewResult,
    _build_review_prompt,
    _collect_review_targets,
    _parse_review_output,
    run_review,
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
                "modules": ["auth", "tasks"],
                "depends_on": [],
                "conventions": [],
            },
        ],
        "2": [
            {
                "node_id": "design:system-design",
                "output": "docs/design/system_design.md",
                "title": "TaskBoard System Design",
                "modules": ["auth", "tasks"],
                "depends_on": [{"id": "req:taskboard-requirements", "relation": "implements"}],
                "conventions": [],
            },
        ],
    },
}


def _setup_project(tmp_path: Path) -> Path:
    """Create a project with config and design docs."""
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(BASE_CONFIG, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    _write_design_doc(
        project / "docs" / "requirements" / "requirements.md",
        node_id="req:taskboard-requirements",
        doc_type="requirement",
        title="TaskBoard Requirements",
        modules=["auth", "tasks"],
        depends_on=[],
        body="## 1. Functional Requirements\n\nUser auth and task management.\n\n## 2. Non-Functional\n\nPerformance targets.\n",
    )
    _write_design_doc(
        project / "docs" / "design" / "system_design.md",
        node_id="design:system-design",
        doc_type="design",
        title="TaskBoard System Design",
        modules=["auth", "tasks"],
        depends_on=[{"id": "req:taskboard-requirements", "relation": "implements"}],
        body="## 1. Overview\n\nSystem overview.\n\n## 2. Architecture\n\nArch details.\n",
    )

    return project


def _write_design_doc(
    path: Path,
    *,
    node_id: str,
    doc_type: str,
    title: str,
    modules: list[str],
    depends_on: list,
    body: str,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    codd_meta: dict = {
        "node_id": node_id,
        "type": doc_type,
        "title": title,
        "modules": modules,
    }
    if depends_on:
        codd_meta["depends_on"] = depends_on
    frontmatter = yaml.safe_dump({"codd": codd_meta}, sort_keys=False)
    path.write_text(f"---\n{frontmatter}---\n\n# {title}\n\n{body}", encoding="utf-8")


PASS_REVIEW_JSON = json.dumps({
    "verdict": "PASS",
    "score": 85,
    "issues": [
        {"severity": "INFO", "message": "Consider adding sequence diagrams."},
    ],
    "feedback": "Well-structured document covering all major components.",
})

FAIL_REVIEW_JSON = json.dumps({
    "verdict": "FAIL",
    "score": 45,
    "issues": [
        {"severity": "CRITICAL", "message": "Missing authentication flow details."},
        {"severity": "WARNING", "message": "No error handling section."},
    ],
    "feedback": "Document needs significant expansion in auth and error handling.",
})


# -- Unit tests: _parse_review_output ----------------------------------------


def test_parse_review_output_pass():
    result = _parse_review_output(
        "design:test", "docs/test.md", "Test", PASS_REVIEW_JSON,
    )
    assert result.verdict == "PASS"
    assert result.score == 85
    assert len(result.issues) == 1
    assert result.issues[0].severity == "INFO"
    assert "sequence diagrams" in result.issues[0].message


def test_parse_review_output_fail():
    result = _parse_review_output(
        "design:test", "docs/test.md", "Test", FAIL_REVIEW_JSON,
    )
    assert result.verdict == "FAIL"
    assert result.score == 45
    assert len(result.issues) == 2
    assert result.issues[0].severity == "CRITICAL"


def test_parse_review_output_strips_markdown_fences():
    fenced = f"```json\n{PASS_REVIEW_JSON}\n```"
    result = _parse_review_output(
        "design:test", "docs/test.md", "Test", fenced,
    )
    assert result.verdict == "PASS"
    assert result.score == 85


def test_parse_review_output_extracts_json_from_text():
    noisy = f"Here is my review:\n{PASS_REVIEW_JSON}\nEnd of review."
    result = _parse_review_output(
        "design:test", "docs/test.md", "Test", noisy,
    )
    assert result.verdict == "PASS"
    assert result.score == 85


def test_parse_review_output_invalid_json_returns_fail():
    result = _parse_review_output(
        "design:test", "docs/test.md", "Test", "not json at all",
    )
    assert result.verdict == "FAIL"
    assert result.score == 0
    assert any(i.severity == "CRITICAL" for i in result.issues)


def test_parse_review_output_critical_caps_score():
    high_score_with_critical = json.dumps({
        "verdict": "PASS",
        "score": 90,
        "issues": [
            {"severity": "CRITICAL", "message": "SQL injection vulnerability."},
        ],
        "feedback": "Good overall but has a critical security issue.",
    })
    result = _parse_review_output(
        "design:test", "docs/test.md", "Test", high_score_with_critical,
    )
    assert result.verdict == "FAIL"
    assert result.score <= 59


def test_parse_review_output_low_score_forces_fail():
    low_pass = json.dumps({
        "verdict": "PASS",
        "score": 70,
        "issues": [],
        "feedback": "Decent but not quite there.",
    })
    result = _parse_review_output(
        "design:test", "docs/test.md", "Test", low_pass,
    )
    assert result.verdict == "FAIL"
    assert result.score == 70


# -- Unit tests: _build_review_prompt ----------------------------------------


def test_build_review_prompt_contains_criteria():
    doc = {
        "node_id": "design:system-design",
        "path": "docs/design/system_design.md",
        "title": "System Design",
        "type": "design",
        "modules": ["auth", "tasks"],
        "depends_on": [],
        "content": "# System Design\n\n## Overview\n\nContent.",
    }
    prompt = _build_review_prompt(doc, [])
    assert "SENIOR TECHNICAL REVIEWER" in prompt
    assert "Architecture soundness" in prompt
    assert "design:system-design" in prompt
    assert "Content." in prompt
    assert "JSON" in prompt


def test_build_review_prompt_includes_upstream_context():
    doc = {
        "node_id": "design:system-design",
        "path": "docs/design/system_design.md",
        "title": "System Design",
        "type": "design",
        "modules": ["auth"],
        "depends_on": [{"id": "req:requirements", "relation": "implements"}],
        "content": "# System Design\n\n## Overview\n\nContent.",
    }
    upstream = [
        {
            "node_id": "req:requirements",
            "title": "Requirements",
            "content_preview": "# Requirements\n\nUser auth needed.",
        },
    ]
    prompt = _build_review_prompt(doc, upstream)
    assert "UPSTREAM DOCUMENTS" in prompt
    assert "req:requirements" in prompt
    assert "User auth needed" in prompt


def test_build_review_prompt_requirement_type_uses_specific_criteria():
    doc = {
        "node_id": "req:test",
        "path": "docs/requirements/requirements.md",
        "title": "Requirements",
        "type": "requirement",
        "modules": [],
        "depends_on": [],
        "content": "# Requirements\n\nContent.",
    }
    prompt = _build_review_prompt(doc, [])
    assert "Testability" in prompt
    assert "Ambiguity" in prompt


# -- Unit tests: _collect_review_targets -------------------------------------


def test_collect_review_targets(tmp_path):
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())

    targets = _collect_review_targets(project, config, scope=None)
    node_ids = {t["node_id"] for t in targets}
    assert "req:taskboard-requirements" in node_ids
    assert "design:system-design" in node_ids


def test_collect_review_targets_with_scope(tmp_path):
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())

    targets = _collect_review_targets(project, config, scope="design:system-design")
    assert len(targets) == 1
    assert targets[0]["node_id"] == "design:system-design"


def test_collect_review_targets_unknown_scope_returns_empty(tmp_path):
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())

    targets = _collect_review_targets(project, config, scope="design:nonexistent")
    assert targets == []


# -- Integration test: run_review with mocked AI -----------------------------


def test_run_review_all_pass(tmp_path, monkeypatch):
    project = _setup_project(tmp_path)

    def fake_run(command, *, input, capture_output, text, check):
        return subprocess.CompletedProcess(
            args=command, returncode=0,
            stdout=PASS_REVIEW_JSON, stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    summary = run_review(project)
    assert summary.pass_count == 2
    assert summary.fail_count == 0
    assert summary.avg_score == 85.0


def test_run_review_mixed_results(tmp_path, monkeypatch):
    project = _setup_project(tmp_path)
    call_count = {"n": 0}

    def fake_run(command, *, input, capture_output, text, check):
        call_count["n"] += 1
        # First doc passes, second fails
        output = PASS_REVIEW_JSON if call_count["n"] == 1 else FAIL_REVIEW_JSON
        return subprocess.CompletedProcess(
            args=command, returncode=0,
            stdout=output, stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    summary = run_review(project)
    assert summary.pass_count == 1
    assert summary.fail_count == 1


def test_run_review_scoped(tmp_path, monkeypatch):
    project = _setup_project(tmp_path)

    def fake_run(command, *, input, capture_output, text, check):
        return subprocess.CompletedProcess(
            args=command, returncode=0,
            stdout=PASS_REVIEW_JSON, stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    summary = run_review(project, scope="design:system-design")
    assert len(summary.results) == 1
    assert summary.results[0].node_id == "design:system-design"


def test_run_review_prompt_includes_upstream(tmp_path, monkeypatch):
    """When reviewing system-design, the prompt should include requirements as upstream."""
    project = _setup_project(tmp_path)
    prompts: list[str] = []

    def fake_run(command, *, input, capture_output, text, check):
        prompts.append(input)
        return subprocess.CompletedProcess(
            args=command, returncode=0,
            stdout=PASS_REVIEW_JSON, stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    run_review(project, scope="design:system-design")
    assert len(prompts) == 1
    # system-design depends_on req:taskboard-requirements
    assert "req:taskboard-requirements" in prompts[0]
    assert "UPSTREAM DOCUMENTS" in prompts[0]


# -- CLI tests ---------------------------------------------------------------


def test_review_cli_text_output(tmp_path, monkeypatch):
    from click.testing import CliRunner

    project = _setup_project(tmp_path)

    def fake_run(command, *, input, capture_output, text, check):
        return subprocess.CompletedProcess(
            args=command, returncode=0,
            stdout=PASS_REVIEW_JSON, stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["review", "--path", str(project)])

    assert "PASS" in result.output
    assert "score: 85" in result.output
    assert "Summary:" in result.output
    assert result.exit_code == 0


def test_review_cli_json_output(tmp_path, monkeypatch):
    from click.testing import CliRunner

    project = _setup_project(tmp_path)

    def fake_run(command, *, input, capture_output, text, check):
        return subprocess.CompletedProcess(
            args=command, returncode=0,
            stdout=PASS_REVIEW_JSON, stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["review", "--path", str(project), "--json"])

    parsed = json.loads(result.output)
    assert parsed["pass_count"] == 2
    assert parsed["avg_score"] == 85.0
    assert len(parsed["results"]) == 2
    assert result.exit_code == 0


def test_review_cli_fail_exit_code(tmp_path, monkeypatch):
    from click.testing import CliRunner

    project = _setup_project(tmp_path)

    def fake_run(command, *, input, capture_output, text, check):
        return subprocess.CompletedProcess(
            args=command, returncode=0,
            stdout=FAIL_REVIEW_JSON, stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["review", "--path", str(project)])

    assert "FAIL" in result.output
    assert result.exit_code == 1


def test_review_cli_no_docs(tmp_path, monkeypatch):
    from click.testing import CliRunner

    project = tmp_path / "empty"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump({
            "version": "0.1.0",
            "project": {"name": "empty", "language": "python"},
            "scan": {"doc_dirs": [], "source_dirs": [], "test_dirs": [], "config_files": [], "exclude": []},
        }, sort_keys=False),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["review", "--path", str(project)])

    assert "No documents found" in result.output
    assert result.exit_code == 0
