from __future__ import annotations

import json
from pathlib import Path

from codd.dag import Node
import codd.llm.best_practice_augmenter as augmenter_module
from codd.llm.best_practice_augmenter import (
    BEST_PRACTICE_AUGMENTERS,
    BestPracticeAugmenter,
    SubprocessAiCommandBestPracticeAugmenter,
    register_best_practice_augmenter,
)
from codd.llm.impl_step_deriver import ImplStep, merge_impl_steps


def _step_payload(step_id: str = "complete_related_concern", **extra) -> dict:
    payload = {
        "id": step_id,
        "kind": "related_completion",
        "rationale": "Complete the omitted related concern.",
        "source_design_section": "best_practice_augmenter",
        "target_path_hint": None,
        "expected_outputs": ["src/related.py"],
        "confidence": 0.91,
        "best_practice_category": "completion",
    }
    payload.update(extra)
    return payload


def _explicit_step() -> object:
    return ImplStep.from_dict(
        {
            "id": "build_declared_contract",
            "kind": "contract_builder",
            "rationale": "Build declared contract.",
            "source_design_section": "docs/design/contract.md",
            "expected_outputs": ["src/contract.py"],
            "approved": True,
        }
    )


def _node() -> Node:
    return Node(
        id="docs/design/contract.md",
        kind="design_doc",
        path="docs/design/contract.md",
        attributes={"content": "# Contract\n\nDeclared behavior.\n"},
    )


class FakeAiCommand:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[str] = []

    def invoke(self, prompt: str, model: str | None = None) -> str:
        self.calls.append(prompt)
        return self.output

    def provider_id(self, model: str | None = None) -> str:
        return "fake_provider"


def test_register_best_practice_augmenter_adds_registry_entry():
    @register_best_practice_augmenter("test_provider")
    class TestAugmenter(BestPracticeAugmenter):
        def suggest_implicit_steps(self, task, design_docs, explicit_steps, project_context):
            return []

    assert BEST_PRACTICE_AUGMENTERS["test_provider"] is TestAugmenter


def test_augmenter_invokes_command_and_marks_steps_inferred(tmp_path: Path):
    fake = FakeAiCommand(json.dumps({"steps": [_step_payload()]}))

    steps = SubprocessAiCommandBestPracticeAugmenter(fake).suggest_implicit_steps(
        {"task_id": "build_contract"},
        [_node()],
        [_explicit_step()],
        {"project_root": tmp_path},
    )

    assert steps[0].inferred is True
    assert steps[0].confidence == 0.91
    assert steps[0].best_practice_category == "completion"
    assert "EXPLICIT STEPS" in fake.calls[0]


def test_augmenter_defaults_missing_category_to_general(tmp_path: Path):
    payload = _step_payload()
    payload.pop("best_practice_category")
    fake = FakeAiCommand(json.dumps({"steps": [payload]}))

    steps = SubprocessAiCommandBestPracticeAugmenter(fake).suggest_implicit_steps(
        {"task_id": "build_contract"},
        [_node()],
        [_explicit_step()],
        {"project_root": tmp_path},
    )

    assert steps[0].best_practice_category == "general"


def test_merge_impl_steps_keeps_explicit_order_and_deduplicates():
    explicit = [_explicit_step()]
    duplicate = ImplStep.from_dict(
        {
            "id": "build_declared_contract",
            "kind": "duplicate",
            "rationale": "Duplicate.",
            "source_design_section": "best_practice_augmenter",
        }
    )
    implicit = [duplicate, ImplStep.from_dict(_step_payload("complete_related_concern", inferred=True))]

    merged = merge_impl_steps(explicit, implicit)

    assert [step.id for step in merged] == ["build_declared_contract", "complete_related_concern"]


def test_builtin_provider_is_registered():
    assert augmenter_module.BEST_PRACTICE_AUGMENTERS["subprocess_ai_command"] is SubprocessAiCommandBestPracticeAugmenter
