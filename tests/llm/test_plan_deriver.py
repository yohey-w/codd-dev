from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.config import load_project_config
from codd.dag import Node
from codd.deployment.providers.ai_command import AiCommandError
import codd.llm.plan_deriver as plan_deriver_module
from codd.llm.plan_deriver import (
    PLAN_DERIVERS,
    DerivedTask,
    DerivedTaskCacheRecord,
    PlanDeriver,
    SubprocessAiCommandPlanDeriver,
    apply_declarative_v_model_layers,
    approve_cached_tasks,
    derived_task_cache_key,
    derived_task_cache_path,
    design_doc_bundle,
    parse_derived_tasks,
    read_derived_task_cache,
    register_plan_deriver,
    utc_timestamp,
    write_derived_task_cache,
)


def _task_payload(task_id: str = "build_contract", **extra) -> dict:
    payload = {
        "id": task_id,
        "title": "Build contract",
        "description": "Create the required contract and verify it.",
        "source_design_doc": "docs/design/contract.md",
        "v_model_layer": "detailed",
        "expected_outputs": ["src/contract.py"],
        "test_kinds": ["unit"],
        "dependencies": [],
    }
    payload.update(extra)
    return payload


def _node(path: str = "docs/design/contract.md", *, content: str = "Body", frontmatter: dict | None = None) -> Node:
    return Node(
        id=path,
        kind="design_doc",
        path=path,
        attributes={"content": content, "frontmatter": frontmatter or {}},
    )


def _write_project(tmp_path: Path, *, config: dict | None = None, body: str = "# Contract\n") -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    payload = {"project": {"name": "demo", "language": "python"}}
    if config:
        payload.update(config)
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    doc = project / "docs" / "design" / "contract.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(body, encoding="utf-8")
    return project


class FakeAiCommand:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[str] = []

    def invoke(self, prompt: str, model: str | None = None) -> str:
        self.calls.append(prompt)
        return self.outputs.pop(0)

    def provider_id(self, model: str | None = None) -> str:
        return "fake_provider"


class FailingAiCommand:
    def invoke(self, prompt: str, model: str | None = None) -> str:
        raise AiCommandError("failed")


def test_derived_task_round_trips_schema():
    task = DerivedTask.from_dict(_task_payload(approved=True, provider_id="fake", generated_at="now"))

    assert DerivedTask.from_dict(task.to_dict()) == task
    assert task.approved is True


def test_derived_task_rejects_invalid_layer():
    with pytest.raises(ValueError, match="v_model_layer"):
        DerivedTask.from_dict(_task_payload(v_model_layer="unknown"))


def test_register_plan_deriver_adds_registry_entry():
    @register_plan_deriver("test_provider")
    class TestDeriver(PlanDeriver):
        def derive_tasks(self, design_docs, v_model_layer, project_context):
            return []

    assert PLAN_DERIVERS["test_provider"] is TestDeriver


def test_parse_derived_tasks_accepts_top_level_list():
    tasks = parse_derived_tasks(
        json.dumps([_task_payload()]),
        provider_id="fake",
        generated_at="now",
    )

    assert tasks[0].id == "build_contract"


def test_parse_derived_tasks_accepts_tasks_object():
    raw = '{"tasks": [{"id": "build_contract", "title": "Build contract", "description": "Do it.", "source_design_doc": "doc", "v_model_layer": "basic", "expected_outputs": [], "test_kinds": ["integration"], "dependencies": []}]}'

    tasks = parse_derived_tasks(raw, provider_id="fake", generated_at="now")

    assert tasks[0].v_model_layer == "basic"


def test_parse_derived_tasks_accepts_markdown_json_fence():
    raw = '```json\n[{"id": "build_contract", "title": "Build contract", "description": "Do it.", "source_design_doc": "doc", "v_model_layer": "requirement", "expected_outputs": [], "test_kinds": ["e2e"], "dependencies": []}]\n```'

    tasks = parse_derived_tasks(raw, provider_id="fake", generated_at="now")

    assert tasks[0].id == "build_contract"


def test_parse_invalid_json_warns_and_returns_empty(caplog):
    tasks = parse_derived_tasks("not-json", provider_id="fake", generated_at="now")

    assert tasks == []
    assert "invalid JSON" in caplog.text


def test_parse_invalid_entry_warns_and_skips(caplog):
    raw = '{"tasks": [{"title": "Missing id"}]}'

    assert parse_derived_tasks(raw, provider_id="fake", generated_at="now") == []
    assert "Skipping derived task" in caplog.text


def test_parse_invalid_test_kind_warns_and_skips(caplog):
    raw = '{"tasks": [{"id": "build_contract", "title": "Build contract", "description": "Do it.", "source_design_doc": "doc", "v_model_layer": "detailed", "expected_outputs": [], "test_kinds": ["manual"], "dependencies": []}]}'

    assert parse_derived_tasks(raw, provider_id="fake", generated_at="now") == []
    assert "unsupported" in caplog.text


def test_declarative_layer_override_from_attributes():
    task = DerivedTask.from_dict(_task_payload(source_design_doc="docs/design/contract.md"))
    node = _node(frontmatter={}, content="Body")
    node.attributes["v_model_layer"] = "requirement"

    result = apply_declarative_v_model_layers([task], [node])

    assert result[0].v_model_layer == "requirement"


def test_declarative_layer_override_from_frontmatter():
    task = DerivedTask.from_dict(_task_payload(source_design_doc="docs/design/contract.md"))
    node = _node(frontmatter={"codd": {"v_model_layer": "basic"}})

    result = apply_declarative_v_model_layers([task], [node])

    assert result[0].v_model_layer == "basic"


def test_design_doc_bundle_uses_node_content(tmp_path):
    bundle = design_doc_bundle([_node(content="Declared body")], {"project_root": tmp_path})

    assert "Declared body" in bundle
    assert "docs/design/contract.md" in bundle


def test_design_doc_bundle_reads_file_content(tmp_path):
    project = _write_project(tmp_path, body="# File body\n")
    node = Node("docs/design/contract.md", "design_doc", "docs/design/contract.md", {"frontmatter": {}})

    bundle = design_doc_bundle([node], {"project_root": project})

    assert "File body" in bundle


def test_cache_key_changes_when_design_sha_changes():
    first = derived_task_cache_key(design_doc_sha="a", provider_id="p", prompt_template_sha="t")
    second = derived_task_cache_key(design_doc_sha="b", provider_id="p", prompt_template_sha="t")

    assert first != second


def test_cache_record_round_trips_yaml(tmp_path):
    path = tmp_path / "cache.yaml"
    record = DerivedTaskCacheRecord("fake", "key", "doc-sha", "template-sha", "now", ["doc"], [DerivedTask.from_dict(_task_payload())])

    write_derived_task_cache(path, record)

    assert read_derived_task_cache(path) == record


def test_deriver_invokes_command_and_writes_cache(tmp_path):
    project = _write_project(tmp_path)
    raw = '{"tasks": [{"id": "build_contract", "title": "Build contract", "description": "Do it.", "source_design_doc": "docs/design/contract.md", "v_model_layer": "detailed", "expected_outputs": [], "test_kinds": ["unit"], "dependencies": []}]}'
    fake = FakeAiCommand([raw])

    tasks = SubprocessAiCommandPlanDeriver(fake).derive_tasks(
        [_node()],
        "detailed",
        {"project_root": project},
    )

    assert tasks[0].id == "build_contract"
    assert derived_task_cache_path([_node()], {"project_root": project}).exists()


def test_deriver_reads_cache_without_invoking_again(tmp_path):
    project = _write_project(tmp_path)
    raw = '{"tasks": [{"id": "build_contract", "title": "Build contract", "description": "Do it.", "source_design_doc": "docs/design/contract.md", "v_model_layer": "detailed", "expected_outputs": [], "test_kinds": ["unit"], "dependencies": []}]}'
    first_fake = FakeAiCommand([raw])
    SubprocessAiCommandPlanDeriver(first_fake).derive_tasks([_node()], "detailed", {"project_root": project})
    second_fake = FakeAiCommand([raw])

    tasks = SubprocessAiCommandPlanDeriver(second_fake).derive_tasks([_node()], "detailed", {"project_root": project})

    assert tasks[0].id == "build_contract"
    assert second_fake.calls == []


def test_deriver_force_bypasses_cache(tmp_path):
    project = _write_project(tmp_path)
    raw = '{"tasks": [{"id": "build_contract", "title": "Build contract", "description": "Do it.", "source_design_doc": "docs/design/contract.md", "v_model_layer": "detailed", "expected_outputs": [], "test_kinds": ["unit"], "dependencies": []}]}'
    fake = FakeAiCommand([raw, raw])
    deriver = SubprocessAiCommandPlanDeriver(fake)

    deriver.derive_tasks([_node()], "detailed", {"project_root": project})
    deriver.derive_tasks([_node()], "detailed", {"project_root": project, "force": True})

    assert len(fake.calls) == 2


def test_deriver_dry_run_does_not_write_cache(tmp_path):
    project = _write_project(tmp_path)
    raw = '{"tasks": [{"id": "build_contract", "title": "Build contract", "description": "Do it.", "source_design_doc": "docs/design/contract.md", "v_model_layer": "detailed", "expected_outputs": [], "test_kinds": ["unit"], "dependencies": []}]}'

    SubprocessAiCommandPlanDeriver(FakeAiCommand([raw])).derive_tasks(
        [_node()],
        "detailed",
        {"project_root": project, "dry_run": True},
    )

    assert not derived_task_cache_path([_node()], {"project_root": project}).exists()


def test_deriver_command_error_returns_empty(tmp_path):
    project = _write_project(tmp_path)

    tasks = SubprocessAiCommandPlanDeriver(FailingAiCommand()).derive_tasks([_node()], "detailed", {"project_root": project})

    assert tasks == []


def test_cache_path_uses_safe_design_doc_name(tmp_path):
    path = derived_task_cache_path([_node("docs/design/my contract.md")], {"project_root": tmp_path})

    assert path.name == "docs_design_my_contract.md.yaml"


def test_config_accepts_plan_derive_command(tmp_path):
    project = _write_project(tmp_path, config={"ai_commands": {"plan_derive": "mock-ai --json"}})

    assert load_project_config(project)["ai_commands"]["plan_derive"] == "mock-ai --json"


def test_cli_plan_derive_dry_run_uses_registered_provider(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path,
        config={"ai_commands": {"plan_derive": {"provider": "fake_cli"}}},
    )

    class FakeDeriver(PlanDeriver):
        def __init__(self, *args, **kwargs):
            pass

        def derive_tasks(self, design_docs, v_model_layer, project_context):
            return [DerivedTask.from_dict(_task_payload())]

    monkeypatch.setitem(plan_deriver_module.PLAN_DERIVERS, "fake_cli", FakeDeriver)

    result = CliRunner().invoke(main, ["plan", "derive", "--path", str(project), "--design-doc", "docs/design/contract.md", "--dry-run"])

    assert result.exit_code == 0
    assert "build_contract" in result.output


def test_cli_plan_derive_missing_provider_is_graceful(tmp_path):
    project = _write_project(tmp_path)

    result = CliRunner().invoke(main, ["plan", "derive", "--path", str(project), "--provider", "missing"])

    assert result.exit_code == 1
    assert "provider not found" in result.output


def test_cli_plan_show_filters_status(tmp_path):
    project = _write_project(tmp_path)
    cache_path = project / ".codd" / "derived_tasks" / "docs_design_contract.md.yaml"
    pending = DerivedTask.from_dict(_task_payload("build_contract"))
    approved = DerivedTask.from_dict(_task_payload("verify_contract", approved=True))
    write_derived_task_cache(
        cache_path,
        DerivedTaskCacheRecord("fake", "key", "doc", "template", "now", ["docs/design/contract.md"], [pending, approved]),
    )

    result = CliRunner().invoke(main, ["plan", "show", "--path", str(project), "--status", "approved"])

    assert result.exit_code == 0
    assert "verify_contract" in result.output
    assert "build_contract" not in result.output


def test_cli_plan_approve_all_and_one_task(tmp_path):
    project = _write_project(tmp_path)
    cache_path = project / ".codd" / "derived_tasks" / "docs_design_contract.md.yaml"
    tasks = [DerivedTask.from_dict(_task_payload("build_contract")), DerivedTask.from_dict(_task_payload("verify_contract"))]
    write_derived_task_cache(
        cache_path,
        DerivedTaskCacheRecord("fake", "key", "doc", "template", "now", ["docs/design/contract.md"], tasks),
    )

    one = CliRunner().invoke(
        main,
        ["plan", "approve", "docs/design/contract.md", "--path", str(project), "--task", "build_contract"],
    )
    after_one = read_derived_task_cache(cache_path)
    all_result = CliRunner().invoke(
        main,
        ["plan", "approve", "docs/design/contract.md", "--path", str(project), "--all"],
    )
    after_all = read_derived_task_cache(cache_path)

    assert one.exit_code == 0
    assert all_result.exit_code == 0
    assert [task.approved for task in after_one.tasks] == [True, False]
    assert all(task.approved for task in after_all.tasks)
