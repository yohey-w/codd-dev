"""Tests for project lexicon prompt injection."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import yaml

import codd.generator as generator_module
import codd.implementer as implementer_module
from codd.generator import _inject_lexicon, generate_wave
from codd.implementer import implement_tasks
from codd.planner import plan_init
from codd.require import run_require


def _lexicon_data() -> dict:
    return {
        "version": "1.0",
        "node_vocabulary": [
            {
                "id": "route_node",
                "description": "Stable browser route identifier",
                "naming_convention": "kebab-case",
            },
        ],
        "naming_conventions": [
            {"id": "kebab-case", "regex": "^[a-z][a-z0-9-]*$"},
        ],
        "design_principles": [
            "Route names must not drift between requirements, designs, and generated code.",
        ],
        "failure_modes": [],
        "extractor_registry": {},
    }


def _write_lexicon(project: Path) -> None:
    (project / "project_lexicon.yaml").write_text(
        yaml.safe_dump(_lexicon_data(), sort_keys=False),
        encoding="utf-8",
    )


def _write_doc(
    project: Path,
    relative_path: str,
    *,
    node_id: str,
    doc_type: str,
    body: str,
    depends_on: list[dict] | None = None,
    source: str | None = None,
) -> None:
    payload = {"codd": {"node_id": node_id, "type": doc_type}}
    if depends_on is not None:
        payload["codd"]["depends_on"] = depends_on
    if source is not None:
        payload["codd"]["source"] = source
    frontmatter = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n\n{body.rstrip()}\n", encoding="utf-8")


def test_inject_lexicon_returns_prompt_unchanged_when_missing(tmp_path):
    base_prompt = "Generate the next artifact.\n"

    assert _inject_lexicon(base_prompt, tmp_path) == base_prompt


def test_inject_lexicon_prepends_context_and_preserves_original_prompt(tmp_path):
    _write_lexicon(tmp_path)
    base_prompt = "Generate the next artifact.\n"

    prompt = _inject_lexicon(base_prompt, tmp_path)

    assert prompt.startswith("## Project Lexicon")
    assert "**route_node**" in prompt
    assert "Stable browser route identifier" in prompt
    assert prompt.endswith("\n\n---\n\n" + base_prompt)


def test_require_ai_prompt_includes_project_lexicon(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _write_lexicon(project)
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "python"},
                "ai_command": "mock-ai --print",
                "scan": {
                    "source_dirs": ["src"],
                    "test_dirs": ["tests"],
                    "doc_dirs": ["docs/requirements/"],
                    "config_files": [],
                    "exclude": [],
                },
                "graph": {"store": "jsonl", "path": "codd/scan"},
                "bands": {
                    "green": {"min_confidence": 0.9, "min_evidence_count": 2},
                    "amber": {"min_confidence": 0.5},
                },
                "propagation": {"max_depth": 10},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    extracted_dir = codd_dir / "extracted" / "modules"
    extracted_dir.mkdir(parents=True)
    _write_doc(
        project,
        "codd/extracted/modules/routes.md",
        node_id="design:extract:routes",
        doc_type="design",
        body="# Routes\n\n- login_route()\n",
        source="extracted",
    )
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="# Routes Requirements\n\n## 1. Overview\n\nRoute requirements.\n",
            stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    run_require(project, scope="routes", force=True)

    assert calls[0].startswith("## Project Lexicon")
    assert "**route_node**" in calls[0]
    assert "login_route()" in calls[0]


def test_generate_ai_prompt_includes_project_lexicon(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _write_lexicon(project)
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "ai_command": "mock-ai --print",
                "project": {"name": "demo", "language": "python"},
                "scan": {
                    "source_dirs": [],
                    "doc_dirs": ["docs/requirements/", "docs/design/"],
                    "exclude": [],
                },
                "graph": {"store": "jsonl", "path": "codd/scan"},
                "wave_config": {
                    "1": [
                        {
                            "node_id": "design:routes",
                            "output": "docs/design/routes.md",
                            "title": "Routes Design",
                            "depends_on": [{"id": "req:routes"}],
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_doc(
        project,
        "docs/requirements/routes.md",
        node_id="req:routes",
        doc_type="requirement",
        body="# Route Requirements\n\nRoutes use stable names.\n",
    )
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="# Routes Design\n\n## 1. Overview\n\nRoute design.\n",
            stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    generate_wave(project, 1)

    assert calls[0].startswith("## Project Lexicon")
    assert "**route_node**" in calls[0]
    assert "Routes use stable names." in calls[0]


def test_implement_ai_prompt_includes_project_lexicon(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _write_lexicon(project)
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "typescript"},
                "ai_command": "mock-ai --print",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_doc(
        project,
        "docs/plan/implementation_plan.md",
        node_id="plan:implementation-plan",
        doc_type="plan",
        body="""# Implementation Plan

#### Sprint 1: Routes

| # | 作業項目 | 対応モジュール | 成果物 |
|---|---|---|---|
| 1-1 | Route helpers | lib/routes | Route helper module |
""",
        depends_on=[],
    )
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        match = re.search(r"Output directory: (?P<output>src/generated/[^\n]+)", input)
        assert match is not None
        output_dir = match.group("output")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                f"=== FILE: {output_dir}/index.ts ===\n"
                "```ts\n"
                "export const routeHelper = true;\n"
                "```\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)

    implement_tasks(project, task="1-1")

    assert calls[0].startswith("## Project Lexicon")
    assert "**route_node**" in calls[0]
    assert "Route helper module" in calls[0]


def test_plan_init_ai_prompt_includes_project_lexicon(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _write_lexicon(project)
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "python"},
                "ai_command": "mock-ai --print",
                "scan": {
                    "source_dirs": [],
                    "doc_dirs": ["docs/requirements/"],
                    "exclude": [],
                },
                "graph": {"store": "jsonl", "path": "codd/scan"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_doc(
        project,
        "docs/requirements/routes.md",
        node_id="req:routes",
        doc_type="requirement",
        body="# Route Requirements\n\nRoutes use stable names.\n",
    )
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                '"1":\n'
                '  - node_id: "design:routes"\n'
                '    output: "docs/design/routes.md"\n'
                '    title: "Routes Design"\n'
                '    depends_on:\n'
                '      - id: "req:routes"\n'
                '        relation: "derives_from"\n'
                '        semantic: "governance"\n'
                '    conventions: []\n'
                '    modules: ["routes"]\n'
            ),
            stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    plan_init(project, force=True)

    assert calls[0].startswith("## Project Lexicon")
    assert "**route_node**" in calls[0]
    assert "Routes use stable names." in calls[0]
