from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from codd.generator import DependencyDocument, WaveArtifact, _build_generation_prompt, generate_wave
from codd.project_types import ProjectCapabilities
from codd.requirements_meta import ALLOWED_UI_PATTERNS


SPEC = WaveArtifact(
    wave=1, node_id="design:operations", output="docs/design/operations.md",
    title="Operations", depends_on=[], conventions=[], owns_operation_flow=True,
)


@pytest.mark.parametrize("capabilities", [
    ProjectCapabilities(user_interface=True), ProjectCapabilities(network_surface="http"),
], ids=["ui", "http"])
def test_design_prompt_lists_ui_pattern_choices_and_allows_omission(capabilities):
    prompt = _build_generation_prompt(SPEC, [], [], capabilities=capabilities, behavior_contract=True)

    assert f"one of {', '.join(sorted(ALLOWED_UI_PATTERNS))}" in prompt
    assert "omit `ui_pattern` if none fits" in prompt.lower()


@pytest.mark.parametrize(("capabilities", "owner_elsewhere"), [
    (ProjectCapabilities(), False), (ProjectCapabilities(user_interface=True), True),
], ids=["no-ui-or-http", "references-owner"])
def test_design_prompt_omits_ui_pattern_when_out_of_scope(capabilities, owner_elsewhere):
    prompt = _build_generation_prompt(
        replace(SPEC, owns_operation_flow=not owner_elsewhere), [], [],
        capabilities=capabilities, behavior_contract=True, operation_flow_owner_elsewhere=owner_elsewhere,
    )

    assert "ui_pattern" not in prompt
    assert ", ".join(sorted(ALLOWED_UI_PATTERNS)) not in prompt


@pytest.mark.parametrize("managed", [True, False], ids=["managed", "declared-in-body"])
def test_rejected_ui_pattern_lists_choices_in_retry_feedback(tmp_path, monkeypatch, managed):
    design = {"node_id": SPEC.node_id, "output": SPEC.output, "title": SPEC.title}
    config = {"wave_config": {1: [design]}}
    if managed:
        config["generate"] = {"design_consistency": True}
        design["owns_operation_flow"] = True
    else:
        config["wave_config"][2] = [{
            "node_id": "design:peer", "output": "docs/design/peer.md", "title": "Peer",
            "behavior_owners": ["peer-spec"],
        }]
    (tmp_path / "codd").mkdir()
    (tmp_path / "codd/codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    valid_body = """## Overview

```yaml
operation_flow:
  operations:
    - id: inspect
    - id: update
      ui_pattern: ""
```

```codd-behavior
owns: {}
cases: []
```
"""
    invalid_body = valid_body.replace("- id: inspect", "- id: inspect\n      ui_pattern: free text")
    replies = iter([invalid_body, valid_body])
    prompts = []

    def reply(command, prompt):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr("codd.generator._invoke_ai_command", reply)
    warning = "unknown operation_flow ui_pattern 'free text' at generated_document.operation_flow.operations[0]"
    with pytest.warns(UserWarning) as warnings:
        generate_wave(tmp_path, 1, ai_command="fake-ai")

    assert [str(item.message) for item in warnings] == [warning]
    assert len(prompts) == 2
    guidance = "Omit `ui_pattern` if none fits."
    assert (guidance in prompts[0]) is managed
    retry, feedback = prompts[1].split("--- REVIEW FEEDBACK", 1)
    assert guidance in retry
    feedback = feedback.split("--- END REVIEW FEEDBACK", 1)[0]
    assert "free text" in feedback and "operations[0]" in feedback
    assert ", ".join(sorted(ALLOWED_UI_PATTERNS)) in feedback


@pytest.mark.parametrize("folder", ["governance", "infra", "operations"])
def test_operation_flow_owner_first_prompt_includes_ui_pattern_guidance(tmp_path, monkeypatch, folder):
    requirement = tmp_path / "docs/requirements/input.md"
    requirement.parent.mkdir(parents=True)
    field_instruction = "An operation may declare `ui_pattern`."
    requirement.write_text(
        f"---\ncodd:\n  node_id: req:input\n  type: requirement\n---\n\n{field_instruction}\n",
        encoding="utf-8",
    )
    owner = {
        "node_id": SPEC.node_id, "output": f"docs/{folder}/operations.md", "title": SPEC.title,
        "depends_on": ["req:input"], "owns_operation_flow": True,
    }
    config = {
        "generate": {"design_consistency": True}, "wave_config": {1: [owner]},
        "scan": {"doc_dirs": ["docs/requirements"]},
    }
    (tmp_path / "codd").mkdir()
    (tmp_path / "codd/codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    prompts = []

    def reply(command, prompt):
        prompts.append(prompt)
        return (
            "## Overview\n\n```yaml\noperation_flow:\n  operations:\n    - id: inspect\n```\n\n"
            "```codd-behavior\nowns: {}\ncases: []\n```\n"
        )

    monkeypatch.setattr("codd.generator._invoke_ai_command", reply)
    generate_wave(tmp_path, 1, ai_command="fake-ai")

    first_prompt, = prompts
    assert field_instruction in first_prompt
    assert f"one of {', '.join(sorted(ALLOWED_UI_PATTERNS))}" in first_prompt
    assert "Omit `ui_pattern` if none fits." in first_prompt


@pytest.mark.parametrize(("strict", "requirement"), [
    (True, "Describe the operation."), (False, "An operation may declare `ui_pattern`."),
], ids=["field-not-listed", "check-disabled"])
def test_ui_pattern_guidance_requires_field_instructions_and_strict_check(strict, requirement):
    owner = replace(SPEC, output="docs/infra/operations.md", title="Review of `ui_pattern`")
    dependency = DependencyDocument("req:input", Path("docs/requirements/input.md"), requirement)
    prompt = _build_generation_prompt(
        owner, [dependency], [], feedback="Check `ui_pattern`.", behavior_contract=strict,
    )

    assert "Omit `ui_pattern` if none fits." not in prompt
