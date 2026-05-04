"""Tests for implementer wrapper callback wiring guidance."""

from __future__ import annotations

from pathlib import Path
import warnings

import pytest

import codd.implementer as implementer_module


def _plan_and_task(
    *,
    title: str = "Build login wrapper",
    summary: str = "Login route wrapper",
    module_hint: str = "app/login/page.tsx",
    deliverable: str = "UI route wrapper",
    task_context: str = "Wrap SignInForm for /login.",
):
    plan = implementer_module.ImplementationPlan(
        node_id="plan:test",
        path=Path("docs/plan/implementation_plan.md"),
        content="# Plan",
        depends_on=[],
        conventions=[],
    )
    task = implementer_module.ImplementationTask(
        task_id="1-1",
        title=title,
        summary=summary,
        module_hint=module_hint,
        deliverable=deliverable,
        output_dir="src/generated/login",
        dependency_node_ids=[],
        task_context=task_context,
    )
    return plan, task


def _build_prompt_for_task(**task_kwargs: str) -> str:
    plan, task = _plan_and_task(**task_kwargs)
    return implementer_module._build_implementation_prompt(
        config={"project": {"name": "demo", "language": "typescript"}},
        plan=plan,
        task=task,
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        screen_flow_content="# Screens\n- /login\n",
        screen_flow_routes=["/login"],
    )


def _wrapper_rules_section(prompt: str) -> str:
    return prompt.split("--- WRAPPER COMPONENT RULES ---", 1)[1].split(
        "--- END WRAPPER RULES ---",
        1,
    )[0]


def test_is_wrapper_task_wrapper_keyword():
    assert implementer_module._is_wrapper_task("Build auth wrapper")


def test_is_wrapper_task_page_keyword():
    assert implementer_module._is_wrapper_task("Build settings page")


def test_is_wrapper_task_non_wrapper():
    assert not implementer_module._is_wrapper_task("Build billing service", "Domain logic")


def test_build_prompt_includes_wrapper_rules_for_wrapper_task():
    prompt = _build_prompt_for_task(title="Build login wrapper")

    assert "--- WRAPPER COMPONENT RULES ---" in prompt
    assert "--- END WRAPPER RULES ---" in prompt


def test_build_prompt_no_wrapper_rules_for_non_ui():
    prompt = _build_prompt_for_task(
        title="Build billing service",
        summary="Billing domain service",
        module_hint="lib/billing/service.ts",
        deliverable="Domain service",
        task_context="Implement invoice calculation.",
    )

    assert "WRAPPER COMPONENT RULES" not in prompt


def test_wrapper_rules_mention_callback_wiring():
    prompt = _build_prompt_for_task()
    wrapper_rules = _wrapper_rules_section(prompt)

    assert "callback" in wrapper_rules.casefold()


def test_wrapper_rules_mention_component_name():
    prompt = _build_prompt_for_task()
    wrapper_rules = _wrapper_rules_section(prompt).casefold()

    assert "component name" in wrapper_rules
    assert "rename" in wrapper_rules


def test_check_guard_files_single_middleware_ok(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "middleware.ts").write_text("export {}\n", encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        implementer_module._check_guard_files_uniqueness(project)

    assert not [
        warning for warning in caught if "Multiple 'middleware.ts'" in str(warning.message)
    ]


def test_check_guard_files_duplicate_middleware_warns(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "middleware.ts").write_text("export {}\n", encoding="utf-8")
    nested = project / "src"
    nested.mkdir()
    (nested / "middleware.ts").write_text("export {}\n", encoding="utf-8")

    with pytest.warns(UserWarning, match="Multiple 'middleware.ts' detected"):
        implementer_module._check_guard_files_uniqueness(project)


def test_check_guard_files_codd_yaml_override(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "auth.guard.ts").write_text("export {}\n", encoding="utf-8")
    nested = project / "src"
    nested.mkdir()
    (nested / "auth.guard.ts").write_text("export {}\n", encoding="utf-8")

    with pytest.warns(UserWarning, match="Multiple 'auth.guard.ts' detected"):
        implementer_module._check_guard_files_uniqueness(
            project,
            {"implementer": {"guard_files": ["auth.guard.ts"]}},
        )


def test_generality_no_nextjs_hardcode_in_wrapper_rules():
    prompt = _build_prompt_for_task()
    wrapper_rules = _wrapper_rules_section(prompt).casefold()

    assert "next.js" not in wrapper_rules
    assert "nextjs" not in wrapper_rules
