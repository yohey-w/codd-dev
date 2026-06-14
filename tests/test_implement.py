"""Tests for codd implement direct design-node API."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from click.testing import CliRunner
import yaml

import codd.implementer as implementer_module
from codd.cli import main
from codd.implementer import DesignContext, ImplementSpec, _build_implementation_prompt


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


def _setup_project(
    tmp_path: Path,
    *,
    language: str = "typescript",
    include_coding_principles: bool = False,
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "codd").mkdir()
    config = {
        "project": {"name": "demo", "language": language, "frameworks": ["nextjs"]},
        "ai_command": "mock-ai --print",
        "scan": {
            "source_dirs": ["src/"],
            "test_dirs": ["tests/"],
            "doc_dirs": ["docs/design/"],
            "config_files": [],
            "exclude": [],
        },
        "conventions": [{"targets": ["db:rls_policies"], "reason": "Tenant isolation is mandatory."}],
        "implement": {"default_output_paths": {"docs/design/auth.md": ["src/auth"]}},
    }
    if include_coding_principles:
        config["coding_principles"] = "docs/governance/coding_principles.md"
        principles_path = project / "docs" / "governance" / "coding_principles.md"
        principles_path.parent.mkdir(parents=True)
        principles_path.write_text(
            "# Coding Principles\n\n- Prefer pure helper functions.\n- Make tenant checks explicit.\n",
            encoding="utf-8",
        )
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _write_doc(
        project,
        "docs/design/shared.md",
        node_id="design:shared",
        body="# Shared Design\n\nUse shared request context.\n",
    )
    _write_doc(
        project,
        "docs/design/auth.md",
        node_id="design:auth",
        depends_on=[{"id": "design:shared", "relation": "depends_on"}],
        conventions=[{"targets": ["module:auth"], "reason": "Role checks are release-blocking."}],
        body="# Auth Design\n\nImplement auth service with tenant-aware checks.\n",
    )
    return project


def _mock_implement_ai(monkeypatch, *, stdout: str | None = None) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        match = re.search(r"Output paths: (?P<output>[^\n,]+)", input)
        assert match is not None
        output_dir = match.group("output")
        calls.append({"command": command, "input": input, "output_dir": output_dir})
        body = stdout
        if body is None:
            body = (
                f"=== FILE: {output_dir}/index.ts ===\n"
                "```ts\n"
                "export type AuthContext = { ready: true };\n"
                "export function buildAuth(): AuthContext {\n"
                "  return { ready: true };\n"
                "}\n"
                "```\n"
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=body, stderr="")

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


def test_implement_command_generates_files_with_traceability_comments(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, include_coding_principles=True)
    calls = _mock_implement_ai(monkeypatch)

    result = CliRunner().invoke(
        main,
        ["implement", "--path", str(project), "--design", "docs/design/auth.md", "--output", "src/auth"],
    )

    assert result.exit_code == 0, result.output
    generated_file = project / "src" / "auth" / "index.ts"
    assert generated_file.exists()
    content = generated_file.read_text(encoding="utf-8")
    assert content.startswith("// @generated-by: codd implement")
    assert "// @generated-from: docs/design/auth.md (design:auth)" in content
    assert "// @generated-from: docs/design/shared.md (design:shared)" in content
    assert "// @design-node: docs/design/auth.md" in content
    assert "1 files generated across 1 task(s)" in result.output

    prompt = calls[0]["input"]
    assert "Project coding principles" in prompt
    assert "Prefer pure helper functions." in prompt
    assert "Tenant isolation is mandatory." in prompt
    assert calls[0]["command"] == ["mock-ai", "--print"]


def test_implement_uses_configured_output_paths(tmp_path, monkeypatch):
    project = _setup_project(tmp_path)
    _mock_implement_ai(monkeypatch)

    result = CliRunner().invoke(main, ["implement", "--path", str(project), "--design", "docs/design/auth.md"])

    assert result.exit_code == 0, result.output
    assert (project / "src" / "auth" / "index.ts").is_file()


def test_implement_respects_python_project_language(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, language="python")
    calls = _mock_implement_ai(
        monkeypatch,
        stdout=(
            "=== FILE: src/auth/service.py ===\n"
            "```python\n"
            "def build_service() -> bool:\n"
            "    return True\n"
            "```\n"
        ),
    )

    result = CliRunner().invoke(
        main,
        ["implement", "--path", str(project), "--design", "docs/design/auth.md", "--output", "src/auth"],
    )

    assert result.exit_code == 0, result.output
    generated_file = project / "src" / "auth" / "service.py"
    assert generated_file.exists()
    assert generated_file.read_text(encoding="utf-8").startswith("# @generated-by: codd implement")
    prompt = calls[0]["input"]
    assert "Primary language: python" in prompt
    assert "Generate concrete production-oriented Python source files." in prompt
    assert "=== FILE: src/auth/<filename>.py ===" in prompt
    assert "```python" in prompt


def test_implement_fallback_uses_rust_extension(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, language="rust")
    calls = _mock_implement_ai(
        monkeypatch,
        stdout=(
            "```rust\n"
            "pub fn build_authentication() -> bool {\n"
            "    true\n"
            "}\n"
            "```\n"
        ),
    )

    result = CliRunner().invoke(
        main,
        ["implement", "--path", str(project), "--design", "docs/design/auth.md", "--output", "src/auth"],
    )

    assert result.exit_code == 0, result.output
    generated_file = project / "src" / "auth" / "index.rs"
    assert generated_file.exists()
    assert generated_file.read_text(encoding="utf-8").startswith("// @generated-by: codd implement")
    assert "Primary language: rust" in calls[0]["input"]
    assert "=== FILE: src/auth/<filename>.rs ===" in calls[0]["input"]


def test_implement_clean_removes_existing_output_path(tmp_path, monkeypatch):
    project = _setup_project(tmp_path)
    stale_dir = project / "src" / "auth"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "stale.ts"
    stale_file.write_text("// stale", encoding="utf-8")
    _mock_implement_ai(monkeypatch)

    result = CliRunner().invoke(
        main,
        ["implement", "--path", str(project), "--design", "docs/design/auth.md", "--output", "src/auth", "--clean"],
    )

    assert result.exit_code == 0, result.output
    assert "Cleaning requested output paths" in result.output
    assert not stale_file.exists()
    assert (project / "src" / "auth" / "index.ts").is_file()


def test_get_valid_task_slugs(tmp_path):
    from codd.implementer import get_valid_task_slugs

    project = _setup_project(tmp_path)

    assert get_valid_task_slugs(project) == {"auth"}


def test_get_valid_task_slugs_no_mapping(tmp_path):
    from codd.implementer import get_valid_task_slugs

    project = tmp_path / "empty"
    project.mkdir()
    (project / "codd").mkdir()
    (project / "codd" / "codd.yaml").write_text("project:\n  name: demo\n  language: typescript\n", encoding="utf-8")

    assert get_valid_task_slugs(project) == set()


def test_error_summaries_excluded_from_prompt():
    prompt = _build_implementation_prompt(
        config={"project": {"language": "typescript", "frameworks": ["next.js"]}},
        design_context=DesignContext(
            node_id="design:test",
            path=Path("docs/design/test.md"),
            content="# Test design\n",
        ),
        spec=ImplementSpec("docs/design/test.md", ["src/test"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        prior_task_outputs=[
            {
                "task_id": "success",
                "task_title": "Successful Task",
                "directory": "src/test",
                "files": ["service.ts"],
                "exported_types": ["User"],
                "exported_functions": [],
                "exported_classes": [],
                "exported_values": [],
            },
            {
                "task_id": "failed",
                "task_title": "Failed Task",
                "directory": "src/failed",
                "files": [],
                "exported_types": [],
                "exported_functions": [],
                "exported_classes": [],
                "exported_values": [],
                "error": "AI command returned empty implementation output",
            },
        ],
    )

    assert "Successful Task" in prompt
    assert "Failed Task" not in prompt
    assert "empty implementation output" not in prompt


def test_implementation_prompt_forbids_meta_copy_in_user_facing_ui():
    prompt = _build_implementation_prompt(
        config={"project": {"language": "typescript", "frameworks": ["next.js"]}},
        design_context=DesignContext(
            node_id="design:login",
            path=Path("docs/design/login.md"),
            content="# Login design\n",
        ),
        spec=ImplementSpec("docs/design/login.md", ["src/app/login/page.tsx"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "production user copy only" in prompt
    assert "never surface design rationale" in prompt
    assert "implementation assumptions" in prompt
    assert "environment notes as visible text" in prompt


def test_test_generation_prompt_guides_scoped_content_assertions():
    """Test-targeting implement runs must steer the model to scope content
    assertions to the subject under test (anti false-RED on incidental markup),
    while keeping the verifiable-behavior coverage requirement intact."""
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python", "frameworks": ["flask"]}},
        design_context=DesignContext(
            node_id="test:acceptance",
            path=Path("docs/test/acceptance_criteria.md"),
            content="# Acceptance criteria\n",
        ),
        spec=ImplementSpec("docs/test/acceptance_criteria.md", ["src/tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    # The scoping principle is present and stated as precision, not weakening.
    assert "Scoped assertions" in prompt
    assert "subject under test" in prompt
    assert "not the whole response body" in prompt
    assert "entire response/document" in prompt
    # The existing coverage requirement must still be enforced (additive, not a
    # replacement): generated tests still assert every declared behavior.
    assert "codd: covers vb=<id>" in prompt
    assert "Still assert the constraint fully" in prompt
    # General principle only — no project/example literals leaked into the guidance.
    for literal in ("kakeibo", "viewport", "initial-scale", "summary"):
        assert literal not in prompt


def test_non_test_generation_prompt_omits_scoped_assertion_block():
    """The scoped-assertion guidance is gated to test-targeting runs only, so a
    plain source-implementation prompt must not carry it."""
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python", "frameworks": ["flask"]}},
        design_context=DesignContext(
            node_id="design:summary",
            path=Path("docs/design/summary.md"),
            content="# Summary design\n",
        ),
        spec=ImplementSpec("docs/design/summary.md", ["src/app"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "Scoped assertions" not in prompt
    assert "codd: covers vb=<id>" not in prompt


def test_test_generation_prompt_emits_e2e_no_runtime_import_rule_for_cli():
    """A test-targeting run on a CLI e2e modality (the default) must steer the
    model to keep the e2e layer subprocess-only and free of runtime imports — the
    e2e-no-runtime-import contract, with an AST-check preference."""
    from codd.project_types import ProjectCapabilities

    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="test:acceptance",
            path=Path("docs/test/acceptance_criteria.md"),
            content="# Acceptance criteria\n",
        ),
        spec=ImplementSpec("docs/test/acceptance_criteria.md", ["src/tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        capabilities=ProjectCapabilities(e2e_modality="cli"),
    )

    assert "E2E no-runtime-import contract" in prompt
    assert "as a SUBPROCESS" in prompt
    # In-process runtime-importing helpers belong in the unit tree, not e2e.
    assert "UNIT/integration helper" in prompt
    # Prefer an AST-based import check over brittle literal-string scanning.
    assert "prefer an AST-based import check" in prompt
    # Stack-neutral: no project/example literals leaked into the guidance.
    for literal in ("todo_cli", "todo-cli", "Playwright", "tests/e2e"):
        assert literal not in prompt


def test_test_generation_prompt_e2e_rule_defaults_on_when_untyped():
    """An untyped run (no capabilities) defaults to the conservative cli baseline,
    so the e2e-no-runtime-import guidance is emitted (matches the gate default)."""
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="test:acceptance",
            path=Path("docs/test/acceptance_criteria.md"),
            content="# Acceptance criteria\n",
        ),
        spec=ImplementSpec("docs/test/acceptance_criteria.md", ["src/tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )
    assert "E2E no-runtime-import contract" in prompt


def test_test_generation_prompt_omits_e2e_rule_for_browser_modality():
    """A browser e2e suite legitimately imports a client/runtime, so the
    no-runtime-import rule MUST be gated out (anti-false guidance)."""
    from codd.project_types import ProjectCapabilities

    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="test:acceptance",
            path=Path("docs/test/acceptance_criteria.md"),
            content="# Acceptance criteria\n",
        ),
        spec=ImplementSpec("docs/test/acceptance_criteria.md", ["src/tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        capabilities=ProjectCapabilities(e2e_modality="browser"),
    )
    assert "E2E no-runtime-import contract" not in prompt
    # The shared test-helper symbol rule (ungated) is still present.
    assert "Test-helper import coherence" in prompt


def test_non_test_generation_prompt_omits_e2e_no_runtime_import_rule():
    """The e2e-contract guidance is gated to test-targeting runs only."""
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="design:summary",
            path=Path("docs/design/summary.md"),
            content="# Summary design\n",
        ),
        spec=ImplementSpec("docs/design/summary.md", ["src/app"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )
    assert "E2E no-runtime-import contract" not in prompt
