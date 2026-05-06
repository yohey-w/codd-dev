from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.dag import Node
from codd.llm.criteria_expander import (
    CRITERIA_EXPANDERS,
    CriteriaExpander,
    CriteriaItem,
    ExpandedCriteria,
    SubprocessAiCommandCriteriaExpander,
    build_criteria_expand_prompt,
    evaluate_expanded_criteria,
    expanded_criteria_cache_path,
    expansion_input_sha256,
    find_task_yaml,
    load_design_docs,
    load_expected_extractions,
    load_task_criteria,
    parse_dynamic_items,
    read_expanded_criteria,
    read_expanded_criteria_cache,
    register_criteria_expander,
    static_criteria_items,
    write_expanded_criteria,
)


def _dynamic_json(dynamic_id: str = "expected_artifact_present") -> str:
    return json.dumps(
        {
            "dynamic_items": [
                {
                    "id": dynamic_id,
                    "text": "Required artifact exists and is verifiable.",
                    "source": "expected_node",
                    "source_ref": "expected_nodes[0]",
                    "severity": "critical",
                }
            ],
            "coverage_summary": {"expected_node_count": 1},
        }
    )


def _node(content: str = "Design content") -> Node:
    return Node("docs/design/spec.md", "design_doc", "docs/design/spec.md", {"content": content})


def test_criteria_item_serializes_roundtrip():
    item = CriteriaItem("c1", "Check it", "expected_node", "n1", "high")

    assert CriteriaItem.from_dict(item.to_dict()) == item


def test_criteria_item_rejects_invalid_source():
    with pytest.raises(ValueError, match="source"):
        CriteriaItem("c1", "Check it", "unknown", "n1", "high")  # type: ignore[arg-type]


def test_criteria_item_rejects_invalid_severity():
    with pytest.raises(ValueError, match="severity"):
        CriteriaItem("c1", "Check it", "expected_node", "n1", "urgent")  # type: ignore[arg-type]


def test_expanded_criteria_serializes_roundtrip():
    expanded = ExpandedCriteria(
        task_id="task_a",
        static_items=static_criteria_items(["Static check"]),
        dynamic_items=[CriteriaItem("d1", "Dynamic check", "expected_edge", "e1", "medium")],
        coverage_summary={"expected_edge_count": 1},
        provider_id="provider",
        generated_at="now",
        input_sha256="abc",
    )

    assert ExpandedCriteria.from_dict(expanded.to_dict()) == expanded


def test_expanded_criteria_rejects_duplicate_ids():
    item = CriteriaItem("same", "Check it", "expected_node", "n1", "high")

    with pytest.raises(ValueError, match="duplicate"):
        ExpandedCriteria("task_a", [item], [item])


def test_register_criteria_expander_decorator():
    @register_criteria_expander("unit_test_expander")
    class UnitTestExpander(CriteriaExpander):
        provider_name = "unit_test"

        def expand(self, task_id, static_criteria, design_docs, expected_extractions, project_context):
            return ExpandedCriteria(task_id, [], [])

    assert CRITERIA_EXPANDERS["unit_test_expander"] is UnitTestExpander


def test_register_criteria_expander_rejects_empty_name():
    with pytest.raises(ValueError, match="name"):
        register_criteria_expander("")


def test_static_criteria_items_are_separated_and_critical():
    items = static_criteria_items(["First", "", "Second"])

    assert [item.id for item in items] == ["static_001", "static_003"]
    assert {item.source for item in items} == {"static"}
    assert {item.severity for item in items} == {"critical"}


def test_parse_dynamic_items_from_object():
    items, summary = parse_dynamic_items(_dynamic_json())

    assert items[0].source == "expected_node"
    assert summary["expected_node_count"] == 1


def test_parse_dynamic_items_from_fenced_json():
    items, _summary = parse_dynamic_items(f"```json\n{_dynamic_json()}\n```")

    assert items[0].id == "expected_artifact_present"


def test_parse_dynamic_items_rejects_static_source():
    raw = json.dumps(
        {
            "dynamic_items": [
                {
                    "id": "bad",
                    "text": "Bad",
                    "source": "static",
                    "source_ref": "completion_criteria[0]",
                    "severity": "medium",
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="source=static"):
        parse_dynamic_items(raw)


def test_build_prompt_includes_inputs(tmp_path):
    template = tmp_path / "template.md"
    template.write_text("{task_id}\n{static_criteria_json}\n{design_doc_bundle}\n{expected_extraction_json}\n", encoding="utf-8")

    prompt = build_criteria_expand_prompt(
        task_id="task_a",
        static_criteria=["Static"],
        design_docs=[_node("Design A")],
        expected_extractions=[{"expected_nodes": [{"path_hint": "src/a.py"}]}],
        project_context={"project_root": tmp_path},
        template_path=template,
    )

    assert "task_a" in prompt
    assert "Static" in prompt
    assert "Design A" in prompt
    assert "src/a.py" in prompt


def test_expand_invokes_ai_and_writes_cache(tmp_path):
    calls = []

    def fake_ai(prompt: str) -> str:
        calls.append(prompt)
        return _dynamic_json()

    expander = SubprocessAiCommandCriteriaExpander(ai_command=fake_ai, project_root=tmp_path)
    result = expander.expand(
        "task_a",
        ["Static"],
        [_node()],
        [{"expected_nodes": [{"path_hint": "src/a.py"}]}],
        {"project_root": tmp_path},
    )

    assert calls
    assert result.static_items[0].source == "static"
    assert result.dynamic_items[0].source == "expected_node"
    assert expanded_criteria_cache_path(tmp_path, "task_a").exists()


def test_expand_uses_cache_on_matching_hash(tmp_path):
    calls = []

    def fake_ai(prompt: str) -> str:
        calls.append(prompt)
        return _dynamic_json()

    expander = SubprocessAiCommandCriteriaExpander(ai_command=fake_ai, project_root=tmp_path)
    args = ("task_a", ["Static"], [_node()], [{"expected_nodes": [{"path_hint": "src/a.py"}]}], {"project_root": tmp_path})

    first = expander.expand(*args)
    second = expander.expand(*args)

    assert first == second
    assert len(calls) == 1


def test_cache_invalidates_when_expected_extraction_changes(tmp_path):
    calls = []

    def fake_ai(prompt: str) -> str:
        calls.append(prompt)
        return _dynamic_json(f"dynamic_{len(calls)}")

    expander = SubprocessAiCommandCriteriaExpander(ai_command=fake_ai, project_root=tmp_path)
    expander.expand("task_a", ["Static"], [_node()], [{"expected_nodes": [{"path_hint": "src/a.py"}]}], {"project_root": tmp_path})
    result = expander.expand(
        "task_a",
        ["Static"],
        [_node()],
        [{"expected_nodes": [{"path_hint": "src/b.py"}]}],
        {"project_root": tmp_path},
    )

    assert len(calls) == 2
    assert result.dynamic_items[0].id == "dynamic_2"


def test_expansion_hash_ignores_static_criteria_by_contract(tmp_path):
    left = expansion_input_sha256("task_a", [_node("A")], [{"expected_nodes": []}], {"project_root": tmp_path})
    right = expansion_input_sha256("task_a", [_node("A")], [{"expected_nodes": []}], {"project_root": tmp_path})

    assert left == right


def test_read_expanded_criteria_cache_rejects_stale_hash(tmp_path):
    path = tmp_path / "criteria.yaml"
    write_expanded_criteria(
        path,
        ExpandedCriteria("task_a", static_criteria_items(["Static"]), [], input_sha256="old"),
    )

    assert read_expanded_criteria_cache(path, "new") is None


def test_load_expected_extractions_from_mapping(tmp_path):
    path = tmp_path / "expected.yaml"
    path.write_text(yaml.safe_dump({"expected_nodes": [{"path_hint": "src/a.py"}]}), encoding="utf-8")

    assert load_expected_extractions([path])[0]["expected_nodes"][0]["path_hint"] == "src/a.py"


def test_load_expected_extractions_from_wrapper(tmp_path):
    path = tmp_path / "expected.yaml"
    path.write_text(
        yaml.safe_dump({"expected_extractions": [{"source_design_doc": "spec.md"}]}),
        encoding="utf-8",
    )

    assert load_expected_extractions([path])[0]["source_design_doc"] == "spec.md"


def test_load_design_docs_from_explicit_path(tmp_path):
    doc = tmp_path / "docs" / "design" / "spec.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("Spec body", encoding="utf-8")

    nodes = load_design_docs(tmp_path, [doc])

    assert nodes[0].attributes["content"] == "Spec body"
    assert nodes[0].path == "docs/design/spec.md"


def test_load_task_criteria_from_list_field(tmp_path):
    task_dir = tmp_path / ".codd" / "tasks"
    task_dir.mkdir(parents=True)
    (task_dir / "task_a.yaml").write_text(
        yaml.safe_dump({"task_id": "task_a", "completion_criteria": ["A", "B"]}),
        encoding="utf-8",
    )

    source = load_task_criteria(tmp_path, "task_a")

    assert source.static_criteria == ["A", "B"]
    assert source.path == task_dir / "task_a.yaml"


def test_load_task_criteria_from_description_heading(tmp_path):
    task_dir = tmp_path / "queue" / "tasks"
    task_dir.mkdir(parents=True)
    (task_dir / "worker.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "task_b",
                "description": "Intro\n\n## Completion Criteria\n- [ ] First\n- [ ] Second\n\n## Notes\n- Ignore",
            }
        ),
        encoding="utf-8",
    )

    source = load_task_criteria(tmp_path, "task_b")

    assert source.static_criteria == ["First", "Second"]


def test_find_task_yaml_by_task_id_inside_known_dirs(tmp_path):
    task_dir = tmp_path / "queue" / "tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "worker.yaml"
    task_path.write_text(yaml.safe_dump({"task_id": "task_c"}), encoding="utf-8")

    assert find_task_yaml(tmp_path, "task_c") == task_path


def test_evaluate_expanded_criteria_reports_counts():
    expanded = ExpandedCriteria(
        "task_a",
        static_criteria_items(["Static"]),
        [CriteriaItem("d1", "Dynamic", "v_model", "layer", "info")],
    )

    report = evaluate_expanded_criteria(expanded)

    assert report["pass_count"] == 2
    assert report["fail_count"] == 0
    assert report["dynamic_count"] == 1


def test_cli_qc_expand_writes_cache(tmp_path, monkeypatch):
    (tmp_path / ".codd").mkdir()
    (tmp_path / ".codd" / "codd.yaml").write_text("ai_command: mock\n", encoding="utf-8")
    task_dir = tmp_path / ".codd" / "tasks"
    task_dir.mkdir()
    (task_dir / "task_a.yaml").write_text(
        yaml.safe_dump({"task_id": "task_a", "completion_criteria": ["Static"]}),
        encoding="utf-8",
    )
    doc = tmp_path / "spec.md"
    doc.write_text("Spec", encoding="utf-8")
    expected = tmp_path / "expected.yaml"
    expected.write_text(yaml.safe_dump({"expected_nodes": [{"path_hint": "src/a.py"}]}), encoding="utf-8")

    monkeypatch.setattr(
        SubprocessAiCommandCriteriaExpander,
        "_invoke",
        lambda self, adapter, prompt, model: _dynamic_json(),
    )

    result = CliRunner().invoke(
        main,
        [
            "qc",
            "expand",
            "--path",
            str(tmp_path),
            "--task",
            "task_a",
            "--design-doc",
            str(doc),
            "--expected-extraction",
            str(expected),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "static=1 dynamic=1" in result.output
    assert expanded_criteria_cache_path(tmp_path, "task_a").exists()


def test_cli_qc_evaluate_report_json(tmp_path):
    (tmp_path / ".codd").mkdir()
    (tmp_path / ".codd" / "codd.yaml").write_text("ai_command: mock\n", encoding="utf-8")
    write_expanded_criteria(
        expanded_criteria_cache_path(tmp_path, "task_a"),
        ExpandedCriteria(
            "task_a",
            static_criteria_items(["Static"]),
            [CriteriaItem("d1", "Dynamic", "expected_edge", "edge", "high")],
        ),
    )

    result = CliRunner().invoke(main, ["qc", "evaluate", "--path", str(tmp_path), "--task", "task_a", "--report-json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 2
    assert payload["fail_count"] == 0


def test_cli_qc_evaluate_text(tmp_path):
    (tmp_path / ".codd").mkdir()
    (tmp_path / ".codd" / "codd.yaml").write_text("ai_command: mock\n", encoding="utf-8")
    write_expanded_criteria(
        expanded_criteria_cache_path(tmp_path, "task_a"),
        ExpandedCriteria("task_a", static_criteria_items(["Static"]), []),
    )

    result = CliRunner().invoke(main, ["qc", "evaluate", "--path", str(tmp_path), "--task", "task_a"])

    assert result.exit_code == 0, result.output
    assert "PASS=1 FAIL=0 TOTAL=1" in result.output


def test_read_expanded_criteria_roundtrip(tmp_path):
    path = tmp_path / "criteria.yaml"
    expanded = ExpandedCriteria("task_a", static_criteria_items(["Static"]), [])

    write_expanded_criteria(path, expanded)

    assert read_expanded_criteria(path) == expanded
