from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import yaml

from codd.cli import main
from codd.llm.impl_step_deriver import (
    ImplStep,
    ImplStepCacheRecord,
    impl_step_cache_path,
    read_impl_step_cache,
    write_impl_step_cache,
)


def _step(step_id: str, *, inferred: bool = False, confidence: float = 1.0, category: str = ""):
    return ImplStep.from_dict(
        {
            "id": step_id,
            "kind": "contract_builder",
            "rationale": f"Create {step_id}.",
            "source_design_section": "docs/design/spec.md#contract",
            "expected_outputs": ["src/contract.py"],
            "provider_id": "fake",
            "generated_at": "now",
            "inferred": inferred,
            "confidence": confidence,
            "best_practice_category": category,
        }
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text("project:\n  name: demo\n", encoding="utf-8")
    return project


def test_cache_write_includes_layer_arrays(tmp_path: Path):
    path = tmp_path / "cache.yaml"
    record = ImplStepCacheRecord(
        "fake",
        "key",
        "task",
        "doc",
        "template",
        "now",
        ["docs/design/spec.md"],
        [_step("explicit_step"), _step("implicit_step", inferred=True, confidence=0.8)],
    )

    write_impl_step_cache(path, record)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert [step["id"] for step in payload["layer_1_steps"]] == ["explicit_step"]
    assert [step["id"] for step in payload["layer_2_steps"]] == ["implicit_step"]


def test_cache_read_accepts_layer_only_payload(tmp_path: Path):
    path = tmp_path / "cache.yaml"
    payload = ImplStepCacheRecord(
        "fake",
        "key",
        "task",
        "doc",
        "template",
        "now",
        ["docs/design/spec.md"],
        [],
    ).to_dict()
    payload.pop("steps")
    payload["layer_1_steps"] = [_step("explicit_step").to_dict()]
    payload["layer_2_steps"] = [_step("implicit_step", inferred=True).to_dict()]
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    record = read_impl_step_cache(path)

    assert [step.id for step in record.steps] == ["explicit_step", "implicit_step"]
    assert [step.inferred for step in record.steps] == [False, True]


def test_implement_steps_show_layer_breakdown(tmp_path: Path):
    project = _project(tmp_path)
    cache_path = impl_step_cache_path("task", {"project_root": project})
    write_impl_step_cache(
        cache_path,
        ImplStepCacheRecord(
            "fake",
            "key",
            "task",
            "doc",
            "template",
            "now",
            ["docs/design/spec.md"],
            [
                _step("explicit_step"),
                _step("implicit_one", inferred=True, confidence=0.9, category="coverage"),
                _step("implicit_two", inferred=True, confidence=0.7, category="error_handling"),
            ],
        ),
    )

    result = CliRunner().invoke(
        main,
        ["implement", "steps", "--task", "task", "--path", str(project), "--show-layer-breakdown"],
    )

    assert result.exit_code == 0
    assert "[Layer 1 - Explicit, from design] (count=1)" in result.output
    assert "[Layer 2 - Best Practice Augment] (count=2, avg_confidence=0.80)" in result.output
    assert "category=coverage" in result.output


def test_implement_steps_default_output_remains_compact(tmp_path: Path):
    project = _project(tmp_path)
    cache_path = impl_step_cache_path("task", {"project_root": project})
    write_impl_step_cache(
        cache_path,
        ImplStepCacheRecord(
            "fake",
            "key",
            "task",
            "doc",
            "template",
            "now",
            ["docs/design/spec.md"],
            [_step("explicit_step"), _step("implicit_step", inferred=True)],
        ),
    )

    result = CliRunner().invoke(main, ["implement", "steps", "--task", "task", "--path", str(project)])

    assert result.exit_code == 0
    assert "explicit_step\tpending\tlayer1" in result.output
    assert "implicit_step\tpending\tlayer2" in result.output
