"""Tests for codd restore (brownfield design doc reconstruction)."""

import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

import codd.generator as generator_module
from codd.cli import main
from codd.generator import WaveArtifact
from codd.planner import ExtractedDocument
from codd.restore import (
    INFERRED_REQUIREMENT_SECTIONS,
    _build_requirement_inference_header,
    _build_restoration_prompt,
    _is_relevant_extracted_doc,
    restore_wave,
)


# -- Fictional TaskBoard app wave_config (brownfield) ----------------------

BROWNFIELD_WAVE_CONFIG = {
    "0": [
        {
            "node_id": "req:taskboard-requirements",
            "output": "docs/requirements/inferred_requirements.md",
            "title": "TaskBoard Inferred Requirements",
            "modules": ["auth", "tasks", "notifications"],
            "depends_on": [
                {"id": "design:extract:system-context", "relation": "derives_from", "semantic": "technical"}
            ],
            "conventions": [],
        },
    ],
    "1": [
        {
            "node_id": "design:acceptance-criteria",
            "output": "docs/test/acceptance_criteria.md",
            "title": "TaskBoard Acceptance Criteria",
            "modules": ["auth", "tasks"],
            "depends_on": [
                {"id": "design:extract:system-context", "relation": "derives_from", "semantic": "technical"}
            ],
            "conventions": [
                {"targets": ["module:auth"], "reason": "Authentication is release-blocking."}
            ],
        },
    ],
    "2": [
        {
            "node_id": "design:system-design",
            "output": "docs/design/system_design.md",
            "title": "TaskBoard System Design",
            "modules": ["auth", "tasks", "notifications"],
            "depends_on": [
                {"id": "design:acceptance-criteria", "relation": "constrained_by", "semantic": "governance"}
            ],
            "conventions": [],
        },
    ],
    "3": [
        {
            "node_id": "design:auth-detail",
            "output": "docs/detailed_design/auth_detail.md",
            "title": "Auth Module Detailed Design",
            "modules": ["auth"],
            "depends_on": [
                {"id": "design:system-design", "relation": "depends_on", "semantic": "technical"}
            ],
            "conventions": [],
        },
    ],
}

BASE_CONFIG = {
    "version": "0.1.0",
    "project": {"name": "taskboard", "language": "python"},
    "ai_command": "mock-ai --print",
    "scan": {
        "source_dirs": ["src"],
        "test_dirs": ["tests"],
        "doc_dirs": ["docs/design/", "docs/detailed_design/", "docs/test/"],
        "config_files": [],
        "exclude": [],
    },
    "graph": {"store": "jsonl", "path": "codd/scan"},
    "bands": {
        "green": {"min_confidence": 0.90, "min_evidence_count": 2},
        "amber": {"min_confidence": 0.50},
    },
    "propagation": {"max_depth": 10},
    "wave_config": BROWNFIELD_WAVE_CONFIG,
}


def _setup_brownfield_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(deepcopy(BASE_CONFIG), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _write_extracted_docs(project)
    return project


def _write_extracted_docs(project: Path):
    extracted_dir = project / "codd" / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    modules_dir = extracted_dir / "modules"
    modules_dir.mkdir(exist_ok=True)

    _write_extracted_doc(
        extracted_dir / "system-context.md",
        node_id="design:extract:system-context",
        title="TaskBoard System Context",
        body=(
            "3 modules: auth, tasks, notifications. 2,400 lines total.\n\n"
            "## Module Overview\n\n"
            "| Module | Files | Lines |\n"
            "|--------|-------|-------|\n"
            "| auth | 3 | 800 |\n"
            "| tasks | 4 | 1,200 |\n"
            "| notifications | 2 | 400 |"
        ),
    )
    _write_extracted_doc(
        modules_dir / "auth.md",
        node_id="design:extract:auth",
        title="auth",
        body=(
            "## Symbol Inventory\n\n"
            "| Kind | Name | Signature |\n"
            "|------|------|-----------|\n"
            "| class | AuthService | — |\n"
            "| class | User | bases: BaseModel |\n"
            "| function | login | login(email: str, password: str) -> Token |\n"
            "| function | verify_token | verify_token(token: str) -> User |"
        ),
        source_files=["src/auth/service.py", "src/auth/models.py"],
    )
    _write_extracted_doc(
        modules_dir / "tasks.md",
        node_id="design:extract:tasks",
        title="tasks",
        body=(
            "## Symbol Inventory\n\n"
            "| Kind | Name | Signature |\n"
            "|------|------|-----------|\n"
            "| class | TaskService | — |\n"
            "| class | Task | bases: BaseModel |\n"
            "| function | create_task | create_task(title: str, assignee: User) -> Task |\n\n"
            "## Import Dependencies\n\n"
            "- auth"
        ),
        source_files=["src/tasks/service.py", "src/tasks/models.py"],
    )
    _write_extracted_doc(
        modules_dir / "notifications.md",
        node_id="design:extract:notifications",
        title="notifications",
        body=(
            "## Symbol Inventory\n\n"
            "| Kind | Name | Signature |\n"
            "|------|------|-----------|\n"
            "| class | NotificationService | — |\n"
            "| function | send_email | async send_email(to: str, subject: str, body: str) -> None |"
        ),
        source_files=["src/notifications/service.py"],
    )


def _write_extracted_doc(
    path: Path,
    *,
    node_id: str,
    title: str,
    body: str,
    source_files: list[str] | None = None,
):
    codd_meta: dict = {
        "node_id": node_id,
        "type": "design",
        "source": "extracted",
        "confidence": 0.75,
        "last_extracted": "2026-03-31",
    }
    if source_files:
        codd_meta["source_files"] = source_files
    frontmatter = yaml.safe_dump({"codd": codd_meta}, sort_keys=False)
    path.write_text(f"---\n{frontmatter}---\n\n# {title}\n\n{body}\n", encoding="utf-8")


# -- AI mock that returns valid restored document body ---------------------


def _make_restored_body(input_text: str) -> str:
    """Build a valid restored doc body from the prompt, respecting sanitize rules."""
    title = "Design Document"
    for line in (input_text or "").splitlines():
        if "Title:" in line:
            title = line.split("Title:")[-1].strip()
            break

    # Detect detailed design — needs Mermaid
    if "docs/detailed_design/" in (input_text or ""):
        return (
            f"# {title}\n\n"
            "## 1. Overview\n\n"
            f"This document describes the existing {title} as extracted from the codebase.\n\n"
            "## 2. Mermaid Diagrams\n\n"
            "```mermaid\n"
            "flowchart TD\n"
            "    A[AuthService] --> B[Token]\n"
            "```\n\n"
            "## 3. Ownership Boundaries\n\n"
            "The auth module owns AuthService.\n\n"
            "## 4. Implementation Implications\n\n"
            "No changes needed.\n\n"
            "## 5. Open Questions\n\n"
            "- No open questions at this time.\n"
        )

    # Detect requirements inference
    if "docs/requirements/" in (input_text or ""):
        return (
            f"# {title}\n\n"
            "## 1. Overview\n\n"
            f"This document describes inferred requirements for {title} based on the existing codebase.\n\n"
            "## 2. Functional Requirements\n\n"
            "- User authentication via email/password [inferred]\n"
            "- Task CRUD operations with assignee support [inferred]\n"
            "- Email notifications for task events [inferred]\n\n"
            "## 3. Non-Functional Requirements\n\n"
            "- Async email sending suggests performance concern [inferred]\n\n"
            "## 4. Constraints\n\n"
            "- Python with BaseModel (Pydantic) for data validation [inferred]\n\n"
            "## 5. Open Questions\n\n"
            "- No open questions at this time.\n"
        )

    return (
        f"# {title}\n\n"
        "## 1. Overview\n\n"
        f"This document describes the existing {title} as extracted from the codebase.\n\n"
        "## 2. Architecture\n\n"
        "The system consists of modules working together.\n\n"
        "## 3. Open Questions\n\n"
        "- No open questions at this time.\n"
    )


@pytest.fixture
def mock_restore_ai(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check):
        stdout = _make_restored_body(input)
        calls.append({"command": command, "input": input})
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)
    return calls


# -- Tests -----------------------------------------------------------------


def test_restore_wave_generates_docs_from_extracted_facts(tmp_path, mock_restore_ai):
    """restore_wave creates design docs using extracted docs as context."""
    project = _setup_brownfield_project(tmp_path)

    results = restore_wave(project, wave=1)

    assert len(results) == 1
    assert results[0].status == "restored"
    assert results[0].node_id == "design:acceptance-criteria"

    # Document was written
    doc_path = project / "docs" / "test" / "acceptance_criteria.md"
    assert doc_path.exists()
    content = doc_path.read_text(encoding="utf-8")
    assert "---" in content  # has frontmatter
    assert "design:acceptance-criteria" in content

    # Prompt was brownfield-specific
    prompt = mock_restore_ai[0]["input"]
    assert "RESTORING" in prompt
    assert "brownfield" in prompt.lower()
    assert "reconstruct" in prompt.lower()


def test_restore_prompt_contains_extracted_facts(tmp_path, mock_restore_ai):
    """Restoration prompt includes extracted document content."""
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=1)

    prompt = mock_restore_ai[0]["input"]
    assert "--- BEGIN EXTRACTED" in prompt
    assert "--- END EXTRACTED" in prompt
    assert "TaskBoard System Context" in prompt
    assert "AuthService" in prompt  # from auth module
    assert "TaskService" in prompt  # from tasks module


def test_restore_prompt_filters_by_modules(tmp_path, mock_restore_ai):
    """Restoration prompt filters extracted docs by artifact's modules field."""
    project = _setup_brownfield_project(tmp_path)

    # Wave 3 has auth-detail which only covers ["auth"]
    restore_wave(project, wave=3)

    prompt = mock_restore_ai[0]["input"]
    assert "AuthService" in prompt  # auth module included
    assert "system-context" in prompt.lower()  # system-context always included
    assert "NotificationService" not in prompt  # notifications module excluded


def test_restore_skips_existing_docs(tmp_path, mock_restore_ai):
    """restore_wave skips documents that already exist unless force=True."""
    project = _setup_brownfield_project(tmp_path)

    # Create the output file first
    doc_path = project / "docs" / "test" / "acceptance_criteria.md"
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text("# existing\n", encoding="utf-8")

    results = restore_wave(project, wave=1)
    assert results[0].status == "skipped"
    assert mock_restore_ai == []  # AI was not called

    # With force, it overwrites
    results = restore_wave(project, wave=1, force=True)
    assert results[0].status == "restored"
    assert len(mock_restore_ai) == 1


def test_restore_raises_when_no_extracted_docs(tmp_path):
    """restore_wave raises when extracted docs don't exist."""
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    config = deepcopy(BASE_CONFIG)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    # No extracted docs created

    with pytest.raises(ValueError, match="no extracted documents found"):
        restore_wave(project, wave=1)


def test_restore_prompt_differs_from_generate_prompt():
    """Restoration prompt is fundamentally different from generation prompt."""
    artifact = WaveArtifact(
        wave=1,
        node_id="design:test",
        output="docs/test/test.md",
        title="Test Doc",
        depends_on=[],
        conventions=[],
        modules=["auth"],
    )
    extracted = [
        ExtractedDocument(
            node_id="design:extract:auth",
            path="codd/extracted/modules/auth.md",
            content="# auth\n\nclass AuthService\n",
        )
    ]

    prompt = _build_restoration_prompt(artifact, extracted)

    # Brownfield-specific language
    assert "RESTORING" in prompt
    assert "brownfield" in prompt.lower()
    assert "RECONSTRUCT" in prompt
    assert "what the system IS" in prompt

    # Contains extracted doc
    assert "BEGIN EXTRACTED" in prompt
    assert "AuthService" in prompt


def test_is_relevant_extracted_doc():
    """Relevance filter includes system-context and matching modules."""
    system_ctx = ExtractedDocument(
        node_id="design:extract:system-context",
        path="codd/extracted/system-context.md",
        content="",
    )
    auth_doc = ExtractedDocument(
        node_id="design:extract:auth",
        path="codd/extracted/modules/auth.md",
        content="",
    )
    tasks_doc = ExtractedDocument(
        node_id="design:extract:tasks",
        path="codd/extracted/modules/tasks.md",
        content="",
    )

    module_set = {"auth"}

    assert _is_relevant_extracted_doc(system_ctx, module_set) is True  # always included
    assert _is_relevant_extracted_doc(auth_doc, module_set) is True  # matches
    assert _is_relevant_extracted_doc(tasks_doc, module_set) is False  # not in set


def test_restore_cli_command(tmp_path, mock_restore_ai):
    """CLI 'codd restore --wave 1' works end-to-end."""
    from click.testing import CliRunner

    project = _setup_brownfield_project(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["restore", "--wave", "1", "--path", str(project)])

    assert result.exit_code == 0
    assert "restored" in result.output.lower()
    assert "acceptance_criteria" in result.output


def test_restore_wave_raises_for_invalid_wave(tmp_path, mock_restore_ai):
    """restore_wave raises for a wave number not in wave_config."""
    project = _setup_brownfield_project(tmp_path)

    with pytest.raises(ValueError, match="wave_config has no entries for wave 99"):
        restore_wave(project, wave=99)


def test_restore_detailed_design_includes_mermaid_guidance(tmp_path, mock_restore_ai):
    """Restoration prompt for detailed design docs includes Mermaid instructions."""
    project = _setup_brownfield_project(tmp_path)

    # Wave 3 is a detailed design doc (docs/detailed_design/auth_detail.md)
    restore_wave(project, wave=3)

    prompt = mock_restore_ai[0]["input"]
    assert "detailed design document" in prompt.lower()
    assert "Mermaid diagrams" in prompt
    assert "```mermaid```" in prompt


# -- Requirements inference tests -------------------------------------------


def test_restore_requirements_generates_inferred_doc(tmp_path, mock_restore_ai):
    """restore_wave creates inferred requirements doc from extracted facts."""
    project = _setup_brownfield_project(tmp_path)

    results = restore_wave(project, wave=0)

    assert len(results) == 1
    assert results[0].status == "restored"
    assert results[0].node_id == "req:taskboard-requirements"

    doc_path = project / "docs" / "requirements" / "inferred_requirements.md"
    assert doc_path.exists()
    content = doc_path.read_text(encoding="utf-8")
    assert "req:taskboard-requirements" in content
    assert "Functional Requirements" in content


def test_restore_requirements_prompt_uses_inference_language(tmp_path, mock_restore_ai):
    """Requirements restoration prompt uses inference-specific language, not design language."""
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=0)

    prompt = mock_restore_ai[0]["input"]
    # Requirements inference-specific
    assert "INFERRING REQUIREMENTS" in prompt
    assert "REVERSE-ENGINEER" in prompt
    assert "inferred requirements" in prompt.lower()
    # Not design restoration language
    assert "RESTORING a design" not in prompt


def test_restore_requirements_prompt_warns_about_limitations(tmp_path, mock_restore_ai):
    """Requirements inference prompt explicitly flags what cannot be known from code."""
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=0)

    prompt = mock_restore_ai[0]["input"]
    assert "planned but never implemented" in prompt
    assert "bugs from intentional behavior" in prompt.lower()
    assert "business context" in prompt.lower()
    assert "[inferred]" in prompt


def test_restore_requirements_uses_inferred_sections():
    """Requirements inference uses dedicated section list, not default requirement sections."""
    artifact = WaveArtifact(
        wave=0,
        node_id="req:test",
        output="docs/requirements/test.md",
        title="Test Requirements",
        depends_on=[],
        conventions=[],
        modules=[],
    )
    extracted = [
        ExtractedDocument(
            node_id="design:extract:system-context",
            path="codd/extracted/system-context.md",
            content="# System\n\n3 modules.\n",
        )
    ]

    prompt = _build_restoration_prompt(artifact, extracted)

    # Uses inferred requirement sections
    for section in INFERRED_REQUIREMENT_SECTIONS:
        assert f"## {INFERRED_REQUIREMENT_SECTIONS.index(section) + 1}. {section}" in prompt
    # Specifically has Functional/Non-Functional/Constraints
    assert "Functional Requirements" in prompt
    assert "Non-Functional Requirements" in prompt
    assert "Constraints" in prompt


def test_restore_requirements_prompt_includes_all_modules(tmp_path, mock_restore_ai):
    """Requirements inference includes all modules when artifact covers all."""
    project = _setup_brownfield_project(tmp_path)

    # Wave 0 covers ["auth", "tasks", "notifications"]
    restore_wave(project, wave=0)

    prompt = mock_restore_ai[0]["input"]
    assert "AuthService" in prompt
    assert "TaskService" in prompt
    assert "NotificationService" in prompt


def test_restore_requirements_final_instruction_differs(tmp_path, mock_restore_ai):
    """Requirements inference final instruction differs from design restoration."""
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=0)
    prompt_req = mock_restore_ai[0]["input"]

    restore_wave(project, wave=2, force=True)
    prompt_design = mock_restore_ai[1]["input"]

    assert "infer the requirements" in prompt_req.lower()
    assert "INFERRED requirements" in prompt_req
    assert "reconstruct the design document" in prompt_design.lower()
