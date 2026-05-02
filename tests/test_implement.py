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


def test_implement_respects_python_project_language(tmp_path, monkeypatch):
    project = _setup_project(
        tmp_path,
        explicit_sprints=True,
        include_coding_principles=False,
        minimal_config=True,
    )
    config_path = project / "codd" / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["project"]["language"] = "python"
    config["project"]["frameworks"] = ["fastapi"]
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check):
        match = re.search(r"Output directory: (?P<output>src/generated/[^\n]+)", input)
        assert match is not None
        output_dir = match.group("output")
        calls.append({"command": command, "input": input})
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                f"=== FILE: {output_dir}/service.py ===\n"
                "```python\n"
                "def build_service() -> bool:\n"
                "    return True\n"
                "```\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["implement", "--path", str(project), "--task", "1-4", "--ai-cmd", "mock-ai --print"],
    )

    assert result.exit_code == 0
    generated_file = project / "src" / "generated" / "authentication" / "service.py"
    assert generated_file.exists()
    content = generated_file.read_text(encoding="utf-8")
    assert content.startswith("# @generated-by: codd implement")

    prompt = calls[0]["input"]
    assert "Primary language: python" in prompt
    assert "Generate concrete production-oriented Python source files." in prompt
    assert "Honor the configured framework stack (fastapi) when relevant." in prompt
    assert "=== FILE: src/generated/authentication/<filename>.py ===" in prompt
    assert "```python" in prompt
    assert "TypeScript / TSX" not in prompt


def test_implement_fallback_uses_rust_extension(tmp_path, monkeypatch):
    project = _setup_project(
        tmp_path,
        explicit_sprints=True,
        include_coding_principles=False,
        minimal_config=True,
    )
    config_path = project / "codd" / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["project"]["language"] = "rust"
    config["project"]["frameworks"] = ["tauri"]
    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check):
        calls.append({"command": command, "input": input})
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                "```rust\n"
                "pub fn build_authentication() -> bool {\n"
                "    true\n"
                "}\n"
                "```\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["implement", "--path", str(project), "--task", "1-4", "--ai-cmd", "mock-ai --print"],
    )

    assert result.exit_code == 0
    generated_file = project / "src" / "generated" / "authentication" / "index.rs"
    assert generated_file.exists()
    content = generated_file.read_text(encoding="utf-8")
    assert content.startswith("// @generated-by: codd implement")

    prompt = calls[0]["input"]
    assert "Primary language: rust" in prompt
    assert "Generate concrete production-oriented Rust source files." in prompt
    assert "Honor the configured framework stack (tauri) when relevant." in prompt
    assert "=== FILE: src/generated/authentication/<filename>.rs ===" in prompt
    assert "```rust" in prompt
    assert "Otherwise prefer .ts files." not in prompt


def test_implement_clean_removes_existing_generated_output(tmp_path, mock_implement_ai):
    """--clean should remove src/generated/ before re-generating."""
    project = _setup_project(tmp_path, explicit_sprints=True, include_coding_principles=False)

    # Pre-populate stale output
    stale_dir = project / "src" / "generated" / "old_task"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "stale.ts"
    stale_file.write_text("// stale", encoding="utf-8")

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
    (codd_dir / "codd.yaml").write_text("project:\n  name: demo\n  language: typescript\n", encoding="utf-8")

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


def test_extract_all_tasks_from_phase_milestones():
    """Phase milestones (#### M1.1 ...) are extracted correctly and take priority."""
    from codd.implementer import ImplementationPlan, _extract_all_tasks

    plan = ImplementationPlan(
        node_id="plan:implementation-plan",
        path=Path("docs/plan/implementation_plan.md"),
        content="""# Implementation Plan

## 1. Overview

Tasks for the LMS project.

## 2. Milestones

### Phase 1: 基盤（2026-04-14 〜 2026-04-30）

#### M1.1 データベーススキーマ（4/14 〜 4/18）

| タスク | 成果物 | 優先度 | 依存 |
|---|---|---|---|
| Prisma スキーマ定義 | `prisma/schema.prisma` | P0 | なし |
| RLS ポリシー | `migrations/rls.sql` | P0 | スキーマ |

#### M1.2 認証基盤（4/18 〜 4/23）

| タスク | 成果物 | 優先度 | 依存 |
|---|---|---|---|
| NextAuth.js 設定 | `src/lib/auth.config.ts` | P0 | DB |

### Phase 2: コース（2026-05-01 〜 2026-05-21）

#### M2.1 コース管理（5/1 〜 5/7）

| タスク | 成果物 | 優先度 | 依存 |
|---|---|---|---|
| コース CRUD | `src/modules/courses/service.ts` | P0 | Phase 1 |

## 3. Testing

#### Sprint 13（6/11〜6/15）: テスト

| # | 作業項目 | 対応モジュール | 成果物 |
|---|---|---|---|
| 13-1 | E2E テスト | tests/ | テスト |
""",
        depends_on=[{"id": "design:system-design", "relation": "depends_on"}],
        conventions=[],
    )

    tasks = _extract_all_tasks(plan)
    assert len(tasks) == 3
    assert tasks[0].task_id == "m1.1"
    assert tasks[0].title == "M1.1 データベーススキーマ"
    assert tasks[1].task_id == "m1.2"
    assert tasks[2].task_id == "m2.1"
    assert "src/generated/m1_1" in tasks[0].output_dir
    assert "Prisma" in tasks[0].task_context


def test_phase_milestones_skip_header_rows():
    """Header rows like 'タスク | 成果物' are not included in deliverables."""
    from codd.implementer import ImplementationPlan, _extract_tasks_from_phase_milestones

    plan = ImplementationPlan(
        node_id="plan:implementation-plan",
        path=Path("docs/plan/implementation_plan.md"),
        content="""# Plan

## 2. Milestones

### Phase 1

#### M1.1 DB スキーマ（4/14 〜 4/18）

| タスク | 成果物 | 優先度 | 依存 |
|---|---|---|---|
| Prisma 定義 | `schema.prisma` | P0 | なし |
""",
        depends_on=[],
        conventions=[],
    )

    tasks = _extract_tasks_from_phase_milestones(plan)
    assert len(tasks) == 1
    assert "タスク" not in tasks[0].title
    assert "`schema.prisma`" in tasks[0].deliverable


def test_group_tasks_by_phase():
    """Tasks are grouped by phase number for parallel execution."""
    from codd.implementer import ImplementationTask, _group_tasks_by_phase

    tasks = [
        ImplementationTask(
            task_id="m1.1", title="DB", summary="", module_hint="",
            deliverable="", output_dir="src/generated/m1_1_db",
            dependency_node_ids=[], task_context="",
        ),
        ImplementationTask(
            task_id="m1.2", title="Auth", summary="", module_hint="",
            deliverable="", output_dir="src/generated/m1_2_auth",
            dependency_node_ids=[], task_context="",
        ),
        ImplementationTask(
            task_id="m2.1", title="Course", summary="", module_hint="",
            deliverable="", output_dir="src/generated/m2_1_course",
            dependency_node_ids=[], task_context="",
        ),
        ImplementationTask(
            task_id="m2.2", title="Material", summary="", module_hint="",
            deliverable="", output_dir="src/generated/m2_2_material",
            dependency_node_ids=[], task_context="",
        ),
        ImplementationTask(
            task_id="m3.1", title="API", summary="", module_hint="",
            deliverable="", output_dir="src/generated/m3_1_api",
            dependency_node_ids=[], task_context="",
        ),
    ]

    groups = _group_tasks_by_phase(tasks)
    assert len(groups) == 3
    assert [t.task_id for t in groups[0]] == ["m1.1", "m1.2"]
    assert [t.task_id for t in groups[1]] == ["m2.1", "m2.2"]
    assert [t.task_id for t in groups[2]] == ["m3.1"]


def test_is_file_writing_agent():
    """Detect file-writing vs stdout agents correctly."""
    from codd.generator import _is_file_writing_agent

    assert _is_file_writing_agent(["codex", "exec", "--full-auto"]) is True
    assert _is_file_writing_agent(["claude", "-p"]) is False
    assert _is_file_writing_agent(["claude", "--print"]) is False
    assert _is_file_writing_agent(["claude", "--dangerously-skip-permissions"]) is True
    assert _is_file_writing_agent(["claude"]) is True
    assert _is_file_writing_agent([]) is False


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


def test_resolve_task_dependencies():
    """Tasks in phase N are blocked by all tasks in phase N-1."""
    from codd.implementer import ImplementationTask, _group_tasks_by_phase, _resolve_task_dependencies

    tasks = [
        ImplementationTask(
            task_id="m1.1", title="DB", summary="", module_hint="",
            deliverable="", output_dir="src/generated/m1_1_db",
            dependency_node_ids=[], task_context="",
        ),
        ImplementationTask(
            task_id="m1.2", title="Auth", summary="", module_hint="",
            deliverable="", output_dir="src/generated/m1_2_auth",
            dependency_node_ids=[], task_context="",
        ),
        ImplementationTask(
            task_id="m2.1", title="Course", summary="", module_hint="",
            deliverable="", output_dir="src/generated/m2_1_course",
            dependency_node_ids=[], task_context="",
        ),
        ImplementationTask(
            task_id="m3.1", title="API", summary="", module_hint="",
            deliverable="", output_dir="src/generated/m3_1_api",
            dependency_node_ids=[], task_context="",
        ),
    ]

    groups = _group_tasks_by_phase(tasks)
    resolved = _resolve_task_dependencies(groups)

    # Phase 1 tasks have no blockers
    assert resolved[0][0].blocked_by_task_ids == ()
    assert resolved[0][1].blocked_by_task_ids == ()

    # Phase 2 is blocked by all phase 1 tasks
    assert resolved[1][0].blocked_by_task_ids == ("m1.1", "m1.2")

    # Phase 3 is blocked by phase 1 + phase 2
    assert resolved[2][0].blocked_by_task_ids == ("m1.1", "m1.2", "m2.1")


def test_check_blockers_passes_when_all_succeed():
    """No blocker error when all upstream tasks succeeded."""
    from codd.implementer import ImplementationTask, ImplementationResult, _check_blockers

    task = ImplementationTask(
        task_id="m2.1", title="Course", summary="", module_hint="",
        deliverable="", output_dir="src/generated/m2_1",
        dependency_node_ids=[], task_context="",
        blocked_by_task_ids=("m1.1", "m1.2"),
    )
    results = [
        ImplementationResult(task_id="m1.1", task_title="DB", output_dir=Path("x"), generated_files=[Path("a.ts")]),
        ImplementationResult(task_id="m1.2", task_title="Auth", output_dir=Path("x"), generated_files=[Path("b.ts")]),
    ]

    assert _check_blockers(task, results) is None


def test_check_blockers_fails_when_upstream_failed():
    """Blocker error when an upstream task produced no files."""
    from codd.implementer import ImplementationTask, ImplementationResult, _check_blockers

    task = ImplementationTask(
        task_id="m2.1", title="Course", summary="", module_hint="",
        deliverable="", output_dir="src/generated/m2_1",
        dependency_node_ids=[], task_context="",
        blocked_by_task_ids=("m1.1", "m1.2"),
    )
    results = [
        ImplementationResult(task_id="m1.1", task_title="DB", output_dir=Path("x"), generated_files=[Path("a.ts")]),
        ImplementationResult(task_id="m1.2", task_title="Auth", output_dir=Path("x"), generated_files=[], error="empty output"),
    ]

    error = _check_blockers(task, results)
    assert error is not None
    assert "m1.2" in error
    assert "m1.1" not in error


def test_check_blockers_cascades():
    """A task skipped due to blockers also blocks downstream tasks."""
    from codd.implementer import ImplementationTask, ImplementationResult, _check_blockers

    results = [
        ImplementationResult(task_id="m1.1", task_title="DB", output_dir=Path("x"), generated_files=[], error="empty"),
        ImplementationResult(
            task_id="m2.1", task_title="Course", output_dir=Path("x"), generated_files=[],
            error="skipped: blocked by failed task(s) m1.1",
        ),
    ]

    task_m3 = ImplementationTask(
        task_id="m3.1", title="API", summary="", module_hint="",
        deliverable="", output_dir="src/generated/m3_1",
        dependency_node_ids=[], task_context="",
        blocked_by_task_ids=("m1.1", "m2.1"),
    )

    error = _check_blockers(task_m3, results)
    assert error is not None
    assert "m1.1" in error
    assert "m2.1" in error


def test_implement_skips_downstream_on_failure(tmp_path, monkeypatch):
    """When phase 1 fails, phase 2+ tasks are skipped with blocker error."""
    import subprocess as sp
    import codd.implementer as impl
    from codd.cli import main

    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()

    config = {
        "project": {"name": "demo", "language": "typescript"},
        "ai_command": "mock-ai",
    }
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8",
    )

    _write_doc(
        project,
        "docs/design/system_design.md",
        node_id="design:system-design",
        doc_type="design",
        body="# System Design\n\nMinimal.\n",
    )

    plan_body = """# Implementation Plan

## 1. Milestones

#### M1.1 — DB Schema

| タスク | 成果物 |
|---|---|
| Prisma schema | schema.prisma |

#### M1.2 — Routes

| タスク | 成果物 |
|---|---|
| API routes | route.ts |

#### M2.1 — Tests

| タスク | 成果物 |
|---|---|
| E2E tests | spec.ts |
"""

    _write_doc(
        project,
        "docs/plan/implementation_plan.md",
        node_id="plan:implementation-plan",
        doc_type="plan",
        depends_on=[{"id": "design:system-design"}],
        body=plan_body,
    )

    call_count = {"n": 0}

    def fake_run(command, *, input, capture_output, text, check):
        call_count["n"] += 1
        task_match = re.search(r"Task ID: (m\d+\.\d+)", input)
        task_id = task_match.group(1) if task_match else "?"

        if task_id == "m1.1":
            return sp.CompletedProcess(
                args=command, returncode=0,
                stdout=(
                    "=== FILE: src/generated/m1_1_db_schema/schema.ts ===\n"
                    "```ts\nexport const schema = true;\n```\n"
                ),
                stderr="",
            )
        # m1.2 fails (empty output)
        return sp.CompletedProcess(
            args=command, returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr(impl.generator_module.subprocess, "run", fake_run)

    runner = CliRunner()
    result = runner.invoke(main, ["implement", "--path", str(project)])

    assert result.exit_code == 1

    # m1.1 succeeded, m1.2 failed, m2.1 should be skipped (not executed)
    assert call_count["n"] == 2  # only m1.1 and m1.2 were sent to AI
    assert "m1.2" in result.output or "m1.2" in (result.stderr_bytes or b"").decode()
    assert "blocked" in result.output.lower() or "skipped" in result.output.lower()


def test_error_summaries_excluded_from_prompt():
    """Failed task summaries should not contaminate downstream prompts."""
    from codd.implementer import (
        ImplementationPlan,
        ImplementationTask,
        _build_implementation_prompt,
    )

    plan = ImplementationPlan(
        node_id="plan:test",
        path=Path("docs/plan/test.md"),
        content="# Test plan\nNo tasks.",
        depends_on=[],
        conventions=[],
    )
    task = ImplementationTask(
        task_id="m2.1",
        title="Test Task",
        summary="Test task summary",
        module_hint="courses",
        deliverable="Service layer",
        output_dir="src/generated/m2_1",
        dependency_node_ids=[],
        task_context="Test context",
    )

    prior_outputs = [
        {
            "task_id": "m1.1",
            "task_title": "Successful Task",
            "directory": "src/generated/m1_1",
            "files": ["service.ts", "types.ts"],
            "exported_types": ["User", "Tenant"],
            "exported_functions": ["createUser"],
            "exported_classes": [],
            "exported_values": [],
        },
        {
            "task_id": "m1.2",
            "task_title": "Failed Task",
            "directory": "src/generated/m1_2",
            "files": [],
            "exported_types": [],
            "exported_functions": [],
            "exported_classes": [],
            "exported_values": [],
            "error": "AI command returned empty implementation output",
        },
    ]

    prompt = _build_implementation_prompt(
        config={"project": {"language": "typescript", "frameworks": ["next.js"]}},
        plan=plan,
        task=task,
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        prior_task_outputs=prior_outputs,
    )

    assert "m1.1" in prompt
    assert "Successful Task" in prompt
    assert "Failed Task" not in prompt
    assert "empty implementation output" not in prompt
