from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codd.dag import Node
from codd.llm.criteria_expander import (
    CriteriaItem,
    SubprocessAiCommandCriteriaExpander,
    build_criteria_expand_prompt,
    coverage_axes_hint,
    evaluate_expanded_criteria,
    expansion_input_sha256,
    parse_dynamic_items,
    static_criteria_items,
)


def _axis(axis_type: str = "display_surface", variant_id: str = "compact_panel") -> dict:
    return {
        "axis_type": axis_type,
        "rationale": "Declared project coverage dimension.",
        "variants": [
            {
                "id": variant_id,
                "label": "Compact panel",
                "attributes": {"size": "small"},
                "criticality": "critical",
            }
        ],
    }


def _axis_item(axis_type: str = "display_surface", variant_id: str = "compact_panel") -> dict:
    return {
        "id": f"{axis_type}_{variant_id}_covered",
        "text": "The declared axis variant is exercised by an independent check.",
        "source": "coverage_axis",
        "source_ref": f"{axis_type}:{variant_id}",
        "severity": "critical",
        "axis_type": axis_type,
        "variant_id": variant_id,
    }


def _node(content: str = "Design") -> Node:
    return Node("docs/design/spec.md", "design_doc", "docs/design/spec.md", {"content": content})


def test_criteria_item_coverage_axis_round_trips():
    item = CriteriaItem.from_dict(_axis_item())

    assert CriteriaItem.from_dict(item.to_dict()) == item
    assert item.source == "coverage_axis"
    assert item.axis_type == "display_surface"
    assert item.variant_id == "compact_panel"


def test_criteria_item_coverage_axis_can_infer_source_ref():
    payload = _axis_item()
    payload.pop("source_ref")

    item = CriteriaItem.from_dict(payload)

    assert item.source_ref == "display_surface:compact_panel"


def test_criteria_item_coverage_axis_requires_axis_fields():
    payload = _axis_item()
    payload.pop("axis_type")

    with pytest.raises(ValueError, match="axis_type"):
        CriteriaItem.from_dict(payload)


def test_parse_dynamic_items_accepts_coverage_axis_items():
    raw = json.dumps(
        {
            "dynamic_items": [_axis_item()],
            "coverage_summary": {"coverage_axis_count": 1},
        }
    )

    items, summary = parse_dynamic_items(raw)

    assert items[0].source == "coverage_axis"
    assert items[0].variant_id == "compact_panel"
    assert summary["coverage_axis_count"] == 1


def test_evaluate_expanded_criteria_does_not_require_axis_fields_for_static_items():
    from codd.llm.criteria_expander import ExpandedCriteria

    report = evaluate_expanded_criteria(
        ExpandedCriteria("task", static_criteria_items(["Static check"]), [CriteriaItem.from_dict(_axis_item())])
    )

    assert report["fail_count"] == 0


def test_coverage_axes_hint_reads_context_axes():
    hint = coverage_axes_hint({"coverage_axes": [_axis()]}, [])

    assert "display_surface" in hint
    assert "compact_panel" in hint


def test_coverage_axes_hint_reads_project_lexicon(tmp_path: Path):
    (tmp_path / "project_lexicon.yaml").write_text(yaml.safe_dump({"coverage_axes": [_axis()]}), encoding="utf-8")

    hint = coverage_axes_hint({"project_root": tmp_path}, [])

    assert "display_surface" in hint
    assert "compact_panel" in hint


def test_coverage_axes_hint_reads_design_frontmatter(tmp_path: Path):
    content = "---\ncoverage_axes:\n  - axis_type: user_role\n    variants:\n      - id: reviewer\n---\n# Spec\n"

    hint = coverage_axes_hint({"project_root": tmp_path}, [_node(content)])

    assert "user_role" in hint
    assert "reviewer" in hint


def test_build_prompt_includes_coverage_axes_hint(tmp_path: Path):
    template = tmp_path / "template.md"
    template.write_text("AXES\n{coverage_axes_hint}\nEND", encoding="utf-8")

    prompt = build_criteria_expand_prompt(
        task_id="task",
        static_criteria=[],
        design_docs=[],
        expected_extractions=[],
        project_context={"coverage_axes": [_axis()]},
        template_path=template,
    )

    assert "display_surface" in prompt
    assert "{coverage_axes_hint}" not in prompt


def test_expansion_hash_changes_when_coverage_axes_change(tmp_path: Path):
    (tmp_path / "project_lexicon.yaml").write_text(yaml.safe_dump({"coverage_axes": [_axis("one_axis")]}), encoding="utf-8")
    first = expansion_input_sha256("task", [], [], {"project_root": tmp_path})
    (tmp_path / "project_lexicon.yaml").write_text(yaml.safe_dump({"coverage_axes": [_axis("other_axis")]}), encoding="utf-8")

    assert expansion_input_sha256("task", [], [], {"project_root": tmp_path}) != first


def test_expander_keeps_axis_variant_dynamic_items(tmp_path: Path):
    fake_output = json.dumps({"dynamic_items": [_axis_item()], "coverage_summary": {"coverage_axis_count": 1}})
    calls: list[str] = []

    def fake_ai(prompt: str) -> str:
        calls.append(prompt)
        return fake_output

    result = SubprocessAiCommandCriteriaExpander(ai_command=fake_ai, project_root=tmp_path).expand(
        "task",
        [],
        [],
        [],
        {"project_root": tmp_path, "coverage_axes": [_axis()]},
    )

    assert calls
    assert result.dynamic_items[0].source == "coverage_axis"
    assert result.dynamic_items[0].axis_type == "display_surface"
