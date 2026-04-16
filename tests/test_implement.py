"""Tests for codd implement."""

from pathlib import Path
import re
import subprocess

from click.testing import CliRunner
import pytest
import yaml

import codd.implementer as implementer_module
from codd.cli import main


def _write_doc(
    project: Path,
    relative_path: str,
    *,
    node_id: str,
    doc_type: str,
    body: str,
    depends_on: list[dict] | None = None,
    conventions: list[dict] | None = None,
):
    doc_path = project / relative_path
    doc_path.parent.mkdir(parents=True, exist_ok=True)

    codd = {"node_id": node_id, "type": doc_type}
    if depends_on is not None:
        codd["depends_on"] = depends_on
    if conventions is not None:
        codd["conventions"] = conventions

    frontmatter = yaml.safe_dump({"codd": codd}, sort_keys=False, allow_unicode=True)
    doc_path.write_text(f"---\n{frontmatter}---\n\n{body.rstrip()}\n", encoding="utf-8")


def _setup_project(
    tmp_path: Path,
    *,
    explicit_sprints: bool,
    include_coding_principles: bool,
    include_detailed_design: bool = False,
    minimal_config: bool = False,
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()

    if minimal_config:
        config = {
            "project": {"name": "demo", "language": "typescript"},
        }
    else:
        config = {
            "project": {"name": "demo", "language": "typescript", "frameworks": ["nextjs", "prisma"]},
            "ai_command": "mock-ai --print",
            "scan": {
                "source_dirs": ["src/"],
                "test_dirs": ["tests/"],
                "doc_dirs": [
                    "docs/requirements/",
                    "docs/design/",
                    "docs/detailed_design/",
                    "docs/plan/",
                    "docs/governance/",
                ],
                "config_files": [],
                "exclude": [],
            },
            "conventions": [
                {
                    "targets": ["db:rls_policies"],
                    "reason": "Tenant isolation is mandatory.",
                }
            ],
        }

    if include_coding_principles:
        config["coding_principles"] = "docs/governance/coding_principles.md"

    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    if include_coding_principles:
        principles_path = project / "docs" / "governance" / "coding_principles.md"
        principles_path.parent.mkdir(parents=True, exist_ok=True)
        principles_path.write_text(
            "# Coding Principles\n\n- Prefer pure helper functions.\n- Make tenant checks explicit.\n",
            encoding="utf-8",
        )

    _write_doc(
        project,
        "docs/requirements/requirements.md",
        node_id="req:project-requirements",
        doc_type="requirement",
        body="# Requirements\n\nTenant isolation and auditable auth are required.\n",
    )
    _write_doc(
        project,
        "docs/design/system_design.md",
        node_id="design:system-design",
        doc_type="design",
        depends_on=[{"id": "req:project-requirements", "relation": "derives_from"}],
        body="# System Design\n\nUse Next.js App Router and shared request context.\n",
    )
    _write_doc(
        project,
        "docs/design/database_design.md",
        node_id="design:database-design",
        doc_type="design",
        depends_on=[{"id": "design:system-design", "relation": "derives_from"}],
        body="# Database Design\n\nUse Prisma and tenant-aware query guards.\n",
    )
    _write_doc(
        project,
        "docs/design/auth_authorization_design.md",
        node_id="design:auth-authorization-design",
        doc_type="design",
        depends_on=[{"id": "design:system-design", "relation": "derives_from"}],
        body="# Auth Design\n\nUse NextAuth-compatible sessions with role checks.\n",
    )
    _write_doc(
        project,
        "docs/design/api_design.md",
        node_id="design:api-design",
        doc_type="design",
        depends_on=[{"id": "design:system-design", "relation": "derives_from"}],
        body="# API Design\n\nEvery request enforces tenant status and request IDs.\n",
    )
    _write_doc(
        project,
        "docs/design/ux_design.md",
        node_id="design:ux-design",
        doc_type="design",
        depends_on=[{"id": "design:system-design", "relation": "derives_from"}],
        body="# UX Design\n\nUse App Router layouts and clear admin navigation.\n",
    )
    if include_detailed_design:
        _write_doc(
            project,
            "docs/detailed_design/shared_domain_model.md",
            node_id="design:shared-domain-model",
            doc_type="design",
            depends_on=[{"id": "design:system-design", "relation": "depends_on"}],
            body=(
                "# Shared Domain Model\n\n"
                "Use a single canonical owner for Role, TenantStatus, and SessionUser.\n"
            ),
        )

    if explicit_sprints:
        plan_body = """# Implementation Plan

## 1. Overview

Tasks for auth and tenant foundations.

#### Sprint 1（4月1日〜4月14日）: 認証・テナント基盤

| # | 作業項目 | 対応モジュール | 成果物 |
|---|---|---|---|
| 1-4 | NextAuth.js v5 設定 | `lib/auth/config.ts` | 認証基盤 |
"""
    else:
        plan_body = """# Implementation Plan

## 1. Overview

Tasks for auth and tenant foundations.

## 3. Milestones（マイルストーン）

| 期間 | マイルストーン | 成果物 |
|---|---|---|
| 2026-04-01〜2026-04-15 | 基盤確立 | Azure App Service / PostgreSQL / Front Door / CDN / Prisma 接続、認証基盤（NextAuth.js）初期化、共通ミドルウェア（`app.tenant_id`、`app.role`） |
"""

    _write_doc(
        project,
        "docs/plan/implementation_plan.md",
        node_id="plan:implementation-plan",
        doc_type="plan",
        depends_on=[
            {"id": "design:system-design", "relation": "depends_on"},
            {"id": "design:database-design", "relation": "depends_on"},
            {"id": "design:auth-authorization-design", "relation": "depends_on"},
            {"id": "design:api-design", "relation": "depends_on"},
            {"id": "design:ux-design", "relation": "depends_on"},
            *(
                [{"id": "design:shared-domain-model", "relation": "depends_on"}]
                if include_detailed_design
                else []
            ),
        ],
        conventions=[
            {
                "targets": ["module:auth"],
                "reason": "Role checks are release-blocking.",
            }
        ],
        body=plan_body,
    )
    return project


@pytest.fixture
def mock_implement_ai(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check):
        match = re.search(r"Output directory: (?P<output>src/generated/[^\n]+)", input)
        assert match is not None
        output_dir = match.group("output")
        symbol = "".join(part.capitalize() for part in output_dir.rsplit("/", maxsplit=1)[-1].split("_"))
        calls.append({"command": command, "input": input, "output_dir": output_dir})
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                f"=== FILE: {output_dir}/index.ts ===\n"
                "```ts\n"
                f"export type {symbol}Context = {{ ready: true }};\n"
                f"export function build{symbol}(): {symbol}Context {{\n"
                "  return { ready: true };\n"
                "}\n"
                f"export class {symbol}Service {{}}\n"
                "```\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


def test_implement_command_generates_files_with_traceability_comments(tmp_path, mock_implement_ai):
    project = _setup_project(tmp_path, explicit_sprints=True, include_coding_principles=True)
    runner = CliRunner()

    result = runner.invoke(main, ["implement", "--path", str(project), "--task", "1-4"])

    assert result.exit_code == 0
    generated_file = project / "src" / "generated" / "authentication" / "index.ts"
    assert generated_file.exists()
    content = generated_file.read_text(encoding="utf-8")
    assert content.startswith("// @generated-by: codd implement")
    assert "// @generated-from: docs/plan/implementation_plan.md (plan:implementation-plan)" in content
    assert "// @generated-from: docs/design/auth_authorization_design.md (design:auth-authorization-design)" in content
    assert "@task-id: 1-4" in content
    assert not (project / ".codd_meta").exists()
    assert "1 files generated across 1 task(s)" in result.output

    prompt = mock_implement_ai[0]["input"]
    assert "Project coding principles" in prompt
    assert "Prefer pure helper functions." in prompt
    assert "Tenant isolation is mandatory." in prompt
    assert mock_implement_ai[0]["command"] == ["mock-ai", "--print"]


def test_implement_falls_back_to_milestone_inference(tmp_path, mock_implement_ai):
    project = _setup_project(
        tmp_path,
        explicit_sprints=False,
        include_coding_principles=False,
        minimal_config=True,
    )
    runner = CliRunner()

    result = runner.invoke(main, ["implement", "--path", str(project), "--ai-cmd", "custom-ai --print"])

    assert result.exit_code == 0
    assert len(mock_implement_ai) >= 1
    assert mock_implement_ai[0]["command"] == ["custom-ai", "--print"]

    generated_dir = project / "src" / "generated"
    assert generated_dir.exists()
    generated_files = list(generated_dir.rglob("index.ts"))
    assert len(generated_files) >= 1

    first_prompt = mock_implement_ai[0]["input"]
    assert "Task ID:" in first_prompt
    assert "files generated across" in result.output


def test_implement_includes_detailed_design_dependency_documents_in_prompt(tmp_path, mock_implement_ai):
    project = _setup_project(
        tmp_path,
        explicit_sprints=True,
        include_coding_principles=False,
        include_detailed_design=True,
    )
    runner = CliRunner()

    result = runner.invoke(main, ["implement", "--path", str(project), "--task", "1-4"])

    assert result.exit_code == 0
    prompt = mock_implement_ai[0]["input"]
    assert "docs/detailed_design/shared_domain_model.md" in prompt
    assert "single canonical owner for Role, TenantStatus, and SessionUser" in prompt


def test_implement_clean_removes_existing_generated_output(tmp_path, mock_implement_ai):
    """--clean should remove src/generated/ before re-generating."""
    project = _setup_project(tmp_path, explicit_sprints=True, include_coding_principles=False)

    # Pre-populate stale output
    stale_dir = project / "src" / "generated" / "old_task"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "stale.ts"
    stale_file.write_text("// stale")

    runner = CliRunner()
    result = runner.invoke(main, ["implement", "--path", str(project), "--clean"])

    assert result.exit_code == 0
    assert "Cleaning" in result.output
    assert not stale_dir.exists()
    assert not stale_file.exists()


def test_get_valid_task_slugs(tmp_path):
    """get_valid_task_slugs returns valid slug set."""
    from codd.implementer import get_valid_task_slugs

    project = _setup_project(tmp_path, explicit_sprints=True, include_coding_principles=False)
    result = get_valid_task_slugs(project)

    assert isinstance(result, set)
    assert len(result) >= 1


def test_get_valid_task_slugs_no_plan(tmp_path):
    """Returns empty set when implementation plan is missing."""
    from codd.implementer import get_valid_task_slugs

    project = tmp_path / "empty"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text("project:\n  name: demo\n  language: typescript\n")

    result = get_valid_task_slugs(project)
    assert result == set()


def test_deduplicate_slugs():
    """Tasks with colliding slugs get task_id suffix."""
    from codd.implementer import ImplementationTask, _deduplicate_slugs

    tasks = [
        ImplementationTask(
            task_id="1-1",
            title="Auth setup",
            summary="Auth setup",
            module_hint="lib/auth",
            deliverable="認証基盤",
            output_dir="src/generated/authentication",
            dependency_node_ids=[],
            task_context="",
        ),
        ImplementationTask(
            task_id="2-1",
            title="Auth middleware",
            summary="Auth middleware",
            module_hint="lib/auth/middleware",
            deliverable="認証ミドルウェア",
            output_dir="src/generated/authentication",
            dependency_node_ids=[],
            task_context="",
        ),
    ]

    result = _deduplicate_slugs(tasks)
    dirs = [t.output_dir for t in result]
    assert dirs[0] != dirs[1]
    assert "1_1" in dirs[0]
    assert "2_1" in dirs[1]


def test_extract_all_tasks_from_sprint_headings():
    """Sprint headings produce flat output_dir without sprint_N prefix."""
    from codd.implementer import ImplementationPlan, _extract_all_tasks

    plan = ImplementationPlan(
        node_id="plan:implementation-plan",
        path=Path("docs/plan/implementation_plan.md"),
        content="""# Implementation Plan

#### Sprint 1（4月1日〜4月14日）: 認証基盤

| # | 作業項目 | 対応モジュール | 成果物 |
|---|---|---|---|
| 1-1 | NextAuth設定 | lib/auth | 認証基盤 |

#### Sprint 2（4月15日〜4月30日）: DB基盤

| # | 作業項目 | 対応モジュール | 成果物 |
|---|---|---|---|
| 2-1 | Prisma DB設計 | lib/db | DB基盤 |
""",
        depends_on=[{"id": "design:system-design", "relation": "depends_on"}],
        conventions=[],
    )

    tasks = _extract_all_tasks(plan)
    assert len(tasks) == 2
    assert tasks[0].output_dir == "src/generated/authentication"
    assert tasks[1].output_dir == "src/generated/database_foundation"
    assert "sprint_" not in tasks[0].output_dir
    assert "sprint_" not in tasks[1].output_dir


def test_no_sprint_in_prompt(tmp_path, mock_implement_ai):
    """Generated prompt should not contain Sprint references."""
    project = _setup_project(tmp_path, explicit_sprints=True, include_coding_principles=False)
    runner = CliRunner()

    result = runner.invoke(main, ["implement", "--path", str(project), "--task", "1-4"])

    assert result.exit_code == 0
    prompt = mock_implement_ai[0]["input"]
    assert "Sprint:" not in prompt
    assert "Sprint title:" not in prompt
    assert "Sprint window:" not in prompt
