from __future__ import annotations

import json
from pathlib import Path

import yaml

from codd.dag import Node
from codd.llm.approval import filter_layer_2_impl_steps
from codd.llm.best_practice_augmenter import SubprocessAiCommandBestPracticeAugmenter
from codd.llm.impl_step_deriver import (
    ImplStep,
    ImplStepCacheRecord,
    SubprocessAiCommandImplStepDeriver,
    parse_impl_steps,
    render_impl_steps_for_prompt,
    write_impl_step_cache,
    read_impl_step_cache,
)


def _axis() -> dict:
    return {
        "axis_type": "access_context",
        "rationale": "Different actors exercise different obligations.",
        "variants": [{"id": "operator", "label": "Operator"}],
    }


def _step_payload(step_id: str = "verify_access_context", **extra) -> dict:
    payload = {
        "id": step_id,
        "kind": "verification",
        "rationale": "Verify the declared access context.",
        "source_design_section": "docs/design/access.md#flow",
        "target_path_hint": "src/access.py",
        "inputs": ["build_access_flow"],
        "expected_outputs": ["tests/access_test.py"],
        "required_axes": ["access_context"],
    }
    payload.update(extra)
    return payload


def _node(content: str = "# Access\n\nUsers authenticate before protected actions.") -> Node:
    return Node("docs/design/access.md", "design_doc", "docs/design/access.md", {"content": content})


class FakeAiCommand:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[str] = []

    def invoke(self, prompt: str, model: str | None = None) -> str:
        self.calls.append(prompt)
        return self.output

    def provider_id(self, model: str | None = None) -> str:
        return "fake_provider"


def _impl_step(**extra) -> object:
    return ImplStep.from_dict(_step_payload(**extra))


def test_impl_step_required_axes_round_trips():
    step = ImplStep.from_dict(_step_payload())

    assert ImplStep.from_dict(step.to_dict()) == step
    assert step.required_axes == ["access_context"]


def test_impl_step_required_axes_accepts_single_string():
    step = ImplStep.from_dict(_step_payload(required_axes="access_context"))

    assert step.required_axes == ["access_context"]


def test_parse_impl_steps_preserves_required_axes():
    steps = parse_impl_steps(json.dumps({"steps": [_step_payload()]}), provider_id="fake", generated_at="now")

    assert steps[0].required_axes == ["access_context"]


def test_cache_roundtrip_preserves_required_axes(tmp_path: Path):
    path = tmp_path / "cache.yaml"
    record = ImplStepCacheRecord("fake", "key", "task", "doc", "template", "now", ["docs/design/access.md"], [_impl_step()])

    write_impl_step_cache(path, record)

    assert read_impl_step_cache(path).steps[0].required_axes == ["access_context"]


def test_render_impl_steps_for_prompt_includes_required_axes():
    rendered = render_impl_steps_for_prompt([_impl_step(provider_id="fake", generated_at="now")])

    assert "required_axes" in rendered
    assert "access_context" in rendered
    assert "provider_id" not in rendered


def test_deriver_prompt_includes_coverage_axes_hint(tmp_path: Path):
    fake = FakeAiCommand(json.dumps({"steps": [_step_payload()]}))

    steps = SubprocessAiCommandImplStepDeriver(fake).derive_steps(
        {"task_id": "access_task"},
        [_node()],
        {"project_root": tmp_path, "coverage_axes": [_axis()]},
    )

    assert steps[0].required_axes == ["access_context"]
    assert "COVERAGE AXES" in fake.calls[0]
    assert "access_context" in fake.calls[0]


def test_deriver_prompt_reads_project_lexicon_axes(tmp_path: Path):
    (tmp_path / "project_lexicon.yaml").write_text(yaml.safe_dump({"coverage_axes": [_axis()]}), encoding="utf-8")
    fake = FakeAiCommand(json.dumps({"steps": [_step_payload()]}))

    SubprocessAiCommandImplStepDeriver(fake).derive_steps(
        {"task_id": "access_task"},
        [_node()],
        {"project_root": tmp_path},
    )

    assert "access_context" in fake.calls[0]


def test_best_practice_augmenter_preserves_inferred_required_axes(tmp_path: Path):
    fake = FakeAiCommand(
        json.dumps(
            {
                "steps": [
                    _step_payload(
                        "infer_auth_security",
                        inferred=True,
                        confidence=0.93,
                        best_practice_category="risk_dimension",
                        required_axes=["auth_security"],
                    )
                ]
            }
        )
    )

    steps = SubprocessAiCommandBestPracticeAugmenter(fake).suggest_implicit_steps(
        {"task_id": "access_task"},
        [_node()],
        [_impl_step(approved=True)],
        {"project_root": tmp_path, "coverage_axes": [_axis()]},
    )

    assert steps[0].inferred is True
    assert steps[0].required_axes == ["auth_security"]
    assert steps[0].confidence == 0.93


def test_best_practice_prompt_declares_axis_inference_schema(tmp_path: Path):
    fake = FakeAiCommand(json.dumps({"steps": [_step_payload("infer_axis", inferred=True, required_axes=["access_context"])]}))

    SubprocessAiCommandBestPracticeAugmenter(fake).suggest_implicit_steps(
        {"task_id": "access_task"},
        [_node()],
        [_impl_step(approved=True)],
        {"project_root": tmp_path, "coverage_axes": [_axis()]},
    )

    assert "required_axes" in fake.calls[0]
    assert "access_context" in fake.calls[0]
    assert "approval gate" in fake.calls[0]


def test_layer2_axis_step_default_requires_approval():
    step = _impl_step(inferred=True, confidence=0.99, required_axes=["auth_security"], approved=False)

    assert filter_layer_2_impl_steps([step], {}) == []


def test_layer2_axis_step_allows_explicit_approval():
    step = _impl_step(inferred=True, confidence=0.2, required_axes=["auth_security"], approved=True)

    assert filter_layer_2_impl_steps([step], {}) == [step]


def test_layer2_axis_auto_without_optin_still_requires_approval():
    step = _impl_step(inferred=True, confidence=0.99, required_axes=["auth_security"], approved=False)
    config = {"implementer": {"layer_2_approval_mode": {"mode": "auto_high_confidence_only", "confidence_threshold": 0.9}}}

    assert filter_layer_2_impl_steps([step], config) == []


def test_layer2_axis_auto_with_double_optin_allows_high_confidence():
    step = _impl_step(inferred=True, confidence=0.99, required_axes=["auth_security"], approved=False)
    config = {
        "implementer": {
            "layer_2_approval_mode": {
                "mode": "auto_high_confidence_only",
                "require_explicit_optin": True,
                "confidence_threshold": 0.9,
            }
        }
    }

    assert filter_layer_2_impl_steps([step], config) == [step]


def test_layer2_axis_auto_with_double_optin_keeps_low_confidence_pending():
    step = _impl_step(inferred=True, confidence=0.89, required_axes=["auth_security"], approved=False)
    config = {
        "implementer": {
            "layer_2_approval_mode": {
                "mode": "auto_high_confidence_only",
                "require_explicit_optin": True,
                "confidence_threshold": 0.9,
            }
        }
    }

    assert filter_layer_2_impl_steps([step], config) == []


def test_layer2_axis_auto_requires_threshold_of_at_least_point_nine():
    step = _impl_step(inferred=True, confidence=0.99, required_axes=["auth_security"], approved=False)
    config = {
        "implementer": {
            "layer_2_approval_mode": {
                "mode": "auto_high_confidence_only",
                "require_explicit_optin": True,
                "confidence_threshold": 0.8,
            }
        }
    }

    assert filter_layer_2_impl_steps([step], config) == []
