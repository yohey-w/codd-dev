"""Tests for screen-flow prompt injection during implementation."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest
import yaml

import codd.implementer as implementer_module
from codd.cli import CoddCLIError
from codd.implementer import DesignContext, ImplementSpec, implement_tasks


def _write_doc(project: Path, relative_path: str, *, node_id: str, body: str) -> None:
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"{yaml.safe_dump({'codd': {'node_id': node_id, 'type': 'design'}}, sort_keys=False)}"
        "---\n\n"
        f"{body.rstrip()}\n",
        encoding="utf-8",
    )


def _setup_project(tmp_path: Path, *, body: str) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "codd").mkdir()
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "typescript"},
                "ai_command": "mock-ai --print",
                "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/design/"], "config_files": [], "exclude": []},
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (project / "DESIGN.md").write_text("---\ncolors:\n  primary: '#0f62fe'\n---\n", encoding="utf-8")
    _write_doc(project, "docs/design/login.md", node_id="design:login", body=body)
    return project


def _write_screen_flow(project: Path, content: str = "# Login\n- /login\n") -> None:
    path = project / "docs" / "extracted" / "screen-flow.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mock_ai(monkeypatch: pytest.MonkeyPatch, stdout: str | None = None) -> list[str]:
    calls: list[str] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        calls.append(input)
        match = re.search(r"Output paths: (?P<output>[^\n,]+)", input)
        output_dir = match.group("output") if match else "src/login"
        generated = stdout
        if generated is None:
            generated = (
                f"=== FILE: {output_dir}/index.tsx ===\n"
                "```tsx\n"
                "export const Page = () => null;\n"
                "```\n"
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=generated, stderr="")

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


def _design_context() -> DesignContext:
    return DesignContext(
        node_id="design:login",
        path=Path("docs/design/login.md"),
        content="# Login\nImplement /login page.\n",
    )


def test_load_screen_flow_found(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _write_screen_flow(project, "# Screens\n- /login\n")

    assert implementer_module._load_screen_flow_for_implementation(project) == "# Screens\n- /login\n"


def test_load_screen_flow_not_found(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()

    with pytest.warns(UserWarning, match="screen-flow.md not found"):
        assert implementer_module._load_screen_flow_for_implementation(project) is None


def test_build_prompt_includes_screen_flow():
    prompt = implementer_module._build_implementation_prompt(
        config={"project": {"name": "demo", "language": "typescript"}},
        design_context=_design_context(),
        spec=ImplementSpec("docs/design/login.md", ["src/login"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        screen_flow_content="# Screens\n- /login\n",
        screen_flow_routes=["/login"],
    )

    assert "--- SCREEN-FLOW (UI ROUTE DEFINITIONS) ---" in prompt
    assert "This UI work must implement the relevant route(s):" in prompt
    assert "- /login" in prompt
    assert "--- END SCREEN-FLOW ---" in prompt


def test_build_prompt_no_screen_flow():
    prompt = implementer_module._build_implementation_prompt(
        config={"project": {"name": "demo", "language": "typescript"}},
        design_context=_design_context(),
        spec=ImplementSpec("docs/design/login.md", ["src/login"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "SCREEN-FLOW" not in prompt


def test_is_ui_task_page_keyword():
    assert implementer_module._is_ui_task("Build settings page")


def test_is_ui_task_login_keyword():
    assert implementer_module._is_ui_task("ログインフォームを実装")


def test_is_ui_task_non_ui():
    assert not implementer_module._is_ui_task("Build billing service")


def test_zero_files_raises_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _setup_project(tmp_path, body="# Billing\nBuild billing service.\n")
    _mock_ai(monkeypatch, stdout="")

    with pytest.raises(CoddCLIError, match="produced 0 generated files"):
        implement_tasks(project, design="docs/design/login.md", output_paths=["src/billing"])


def test_zero_files_with_skip_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _setup_project(tmp_path, body="skip_generation: true\n")
    _mock_ai(monkeypatch, stdout="")

    results = implement_tasks(project, design="docs/design/login.md", output_paths=["src/login"])

    assert len(results) == 1
    assert results[0].generated_files == []
    assert results[0].error is None


def test_ui_task_includes_route_in_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _setup_project(tmp_path, body="# Login\nBuild login page at /login.\n")
    _write_screen_flow(project, "# Routes\n- /\n- /login\n- /dashboard\n")
    calls = _mock_ai(monkeypatch)

    implement_tasks(project, design="docs/design/login.md", output_paths=["src/login"])

    assert "--- SCREEN-FLOW (UI ROUTE DEFINITIONS) ---" in calls[0]
    assert "This UI work must implement the relevant route(s):" in calls[0]
    assert "- /login" in calls[0]


def test_screen_flow_truncated_at_8000_chars():
    screen_flow = "A" * 8000 + "TAIL"

    prompt = implementer_module._build_implementation_prompt(
        config={"project": {"name": "demo", "language": "typescript"}},
        design_context=_design_context(),
        spec=ImplementSpec("docs/design/login.md", ["src/login"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        screen_flow_content=screen_flow,
    )

    assert "A" * 8000 in prompt
    assert "TAIL" not in prompt


def test_generality_no_framework_specific_code():
    framework_specific = {"react", "nextjs", "next.js", "vue", "svelte", "nuxt", "angular"}

    assert implementer_module._UI_TASK_KEYWORDS.isdisjoint(framework_specific)
