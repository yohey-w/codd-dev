from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest
import yaml

from codd.cli import main
from codd.config import load_project_config
from codd.dag import Node
from codd.deployment.providers.ai_command import AiCommandError
import codd.llm.impl_step_deriver as impl_step_module
from codd.llm.impl_step_deriver import (
    IMPL_STEP_DERIVERS,
    ImplStep,
    ImplStepCacheRecord,
    ImplStepDeriver,
    SubprocessAiCommandImplStepDeriver,
    approve_cached_impl_steps,
    impl_step_cache_key,
    impl_step_cache_path,
    implementation_step_catalog_hint,
    parse_impl_steps,
    read_impl_step_cache,
    register_impl_step_deriver,
    render_impl_steps_for_prompt,
    resolve_implementation_step_catalog,
    write_impl_step_cache,
)


def _step_payload(step_id: str = "build_contract", **extra) -> dict:
    payload = {
        "id": step_id,
        "kind": "contract_builder",
        "rationale": "Build the declared contract.",
        "source_design_section": "docs/design/contract.md#contract",
        "target_path_hint": "src/contract.py",
        "inputs": ["prepare_contract"],
        "expected_outputs": ["src/contract.py"],
    }
    payload.update(extra)
    return payload


def _node(path: str = "docs/design/contract.md", *, content: str = "Body") -> Node:
    return Node(id=path, kind="design_doc", path=path, attributes={"content": content})


def _write_project(tmp_path: Path, *, config: dict | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    payload = {
        "project": {"name": "demo", "language": "python"},
        "scan": {"doc_dirs": ["docs/design/", "docs/plan/"], "source_dirs": ["src/"]},
    }
    if config:
        payload.update(config)
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    design_doc = project / "docs" / "design" / "contract.md"
    design_doc.parent.mkdir(parents=True)
    design_doc.write_text(
        "---\ncodd:\n  node_id: design:contract\n  type: design\n---\n\n# Contract\n",
        encoding="utf-8",
    )
    plan_doc = project / "docs" / "plan" / "implementation_plan.md"
    plan_doc.parent.mkdir(parents=True)
    plan_doc.write_text(
        """---
codd:
  node_id: plan:implementation-plan
  type: plan
  depends_on:
    - id: design:contract
      relation: depends_on
---

# Implementation Plan

#### Sprint 1: Contract

| # | 作業項目 | 対応モジュール | 成果物 |
|---|---|---|---|
| 1-1 | Build contract | src/contract.py | Contract |
""",
        encoding="utf-8",
    )
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


def test_impl_step_round_trips_schema():
    step = ImplStep.from_dict(_step_payload(approved=True, provider_id="fake", generated_at="now"))

    assert ImplStep.from_dict(step.to_dict()) == step
    assert step.inputs == ["prepare_contract"]
    assert step.approved is True


def test_impl_step_rejects_invalid_id():
    with pytest.raises(ValueError, match="snake_case"):
        ImplStep.from_dict(_step_payload("Bad ID"))


def test_register_impl_step_deriver_adds_registry_entry():
    @register_impl_step_deriver("test_provider")
    class TestDeriver(ImplStepDeriver):
        def derive_steps(self, task, design_docs, project_context):
            return []

    assert IMPL_STEP_DERIVERS["test_provider"] is TestDeriver


def test_parse_impl_steps_accepts_top_level_list():
    steps = parse_impl_steps(json.dumps([_step_payload()]), provider_id="fake", generated_at="now")

    assert steps[0].id == "build_contract"


def test_parse_impl_steps_accepts_steps_object_and_dependencies_alias():
    payload = _step_payload(dependencies=["first"])
    payload.pop("inputs")

    steps = parse_impl_steps(json.dumps({"steps": [payload]}), provider_id="fake", generated_at="now")

    assert steps[0].inputs == ["first"]


def test_parse_impl_steps_accepts_markdown_json_fence():
    raw = "```json\n" + json.dumps({"implementation_steps": [_step_payload("fenced_step")]}) + "\n```"

    steps = parse_impl_steps(raw, provider_id="fake", generated_at="now")

    assert steps[0].id == "fenced_step"


def test_parse_invalid_json_warns_and_returns_empty(caplog):
    assert parse_impl_steps("not-json", provider_id="fake", generated_at="now") == []
    assert "invalid JSON" in caplog.text


def test_parse_invalid_entry_warns_and_skips(caplog):
    assert parse_impl_steps(json.dumps({"steps": [{"id": "missing_kind"}]}), provider_id="fake", generated_at="now") == []
    assert "Skipping implementation step" in caplog.text


def test_cache_key_changes_when_task_changes():
    first = impl_step_cache_key(task_id="one", design_doc_sha="a", provider_id="p", prompt_template_sha="t")
    second = impl_step_cache_key(task_id="two", design_doc_sha="a", provider_id="p", prompt_template_sha="t")

    assert first != second


def test_cache_record_round_trips_yaml(tmp_path):
    path = tmp_path / "cache.yaml"
    record = ImplStepCacheRecord("fake", "key", "task", "doc", "template", "now", ["doc"], [ImplStep.from_dict(_step_payload())])

    write_impl_step_cache(path, record)

    assert read_impl_step_cache(path) == record


def test_approve_cached_impl_steps_updates_one_step(tmp_path):
    path = tmp_path / "cache.yaml"
    steps = [ImplStep.from_dict(_step_payload("one")), ImplStep.from_dict(_step_payload("two"))]
    write_impl_step_cache(path, ImplStepCacheRecord("fake", "key", "task", "doc", "template", "now", ["doc"], steps))

    changed = approve_cached_impl_steps(path, step_id="one")
    record = read_impl_step_cache(path)

    assert changed == 1
    assert [step.approved for step in record.steps] == [True, False]


def test_deriver_invokes_command_and_writes_cache(tmp_path):
    project = _write_project(tmp_path)
    fake = FakeAiCommand([json.dumps({"steps": [_step_payload()]})])

    steps = SubprocessAiCommandImplStepDeriver(fake).derive_steps(
        {"task_id": "build_contract", "title": "Build"},
        [_node()],
        {"project_root": project},
    )

    assert steps[0].id == "build_contract"
    assert impl_step_cache_path("build_contract", {"project_root": project}).exists()
    assert "STEP CATALOG HINT" in fake.calls[0]


def test_deriver_reads_cache_without_invoking_again(tmp_path):
    project = _write_project(tmp_path)
    raw = json.dumps({"steps": [_step_payload()]})
    SubprocessAiCommandImplStepDeriver(FakeAiCommand([raw])).derive_steps({"task_id": "build_contract"}, [_node()], {"project_root": project})
    second = FakeAiCommand([raw])

    steps = SubprocessAiCommandImplStepDeriver(second).derive_steps({"task_id": "build_contract"}, [_node()], {"project_root": project})

    assert steps[0].id == "build_contract"
    assert second.calls == []


def test_deriver_command_error_returns_empty(tmp_path):
    project = _write_project(tmp_path)

    assert SubprocessAiCommandImplStepDeriver(FailingAiCommand()).derive_steps({"task_id": "build_contract"}, [_node()], {"project_root": project}) == []


def test_catalog_default_contains_five_domain_hints():
    catalog = resolve_implementation_step_catalog({})

    assert len(catalog["catalog"]) == 5


def test_catalog_project_lexicon_override(tmp_path):
    project = _write_project(tmp_path)
    (project / "project_lexicon.yaml").write_text(
        yaml.safe_dump({"implementation_step_catalog": {"custom": ["one"]}}),
        encoding="utf-8",
    )

    assert resolve_implementation_step_catalog({"project_root": project}) == {"custom": ["one"]}


def test_catalog_config_path_override(tmp_path):
    project = _write_project(tmp_path)
    custom = project / "custom.yaml"
    custom.write_text("catalog:\n  custom: [one]\n", encoding="utf-8")

    result = resolve_implementation_step_catalog(
        {"project_root": project, "config": {"llm": {"implementation_step_catalog_path": "custom.yaml"}}}
    )

    assert result == {"catalog": {"custom": ["one"]}}


def test_render_impl_steps_for_prompt_strips_provider_metadata():
    rendered = render_impl_steps_for_prompt([ImplStep.from_dict(_step_payload(provider_id="fake", generated_at="now"))])

    assert "provider_id" not in rendered
    assert "build_contract" in rendered


def test_config_accepts_impl_step_derive_command(tmp_path):
    project = _write_project(tmp_path, config={"ai_commands": {"impl_step_derive": "mock-ai --json"}})

    assert load_project_config(project)["ai_commands"]["impl_step_derive"] == "mock-ai --json"


def test_cli_implement_plan_uses_registered_provider(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path,
        config={"ai_commands": {"impl_step_derive": {"provider": "fake_cli"}}},
    )

    class FakeDeriver(ImplStepDeriver):
        def __init__(self, *args, **kwargs):
            pass

        def derive_steps(self, task, design_docs, project_context):
            return [ImplStep.from_dict(_step_payload())]

    monkeypatch.setitem(impl_step_module.IMPL_STEP_DERIVERS, "fake_cli", FakeDeriver)

    result = CliRunner().invoke(main, ["implement", "plan", "--path", str(project), "--task", "1-1", "--dry-run"])

    assert result.exit_code == 0
    assert "build_contract" in result.output
