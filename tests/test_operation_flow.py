from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.dag import Node
from codd.dag.builder import build_dag
from codd.llm.criteria_expander import build_criteria_expand_prompt, operation_flow_hint


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _doc_with_frontmatter(frontmatter: dict, body: str = "# Requirement\n") -> str:
    return yaml.safe_dump(frontmatter, explicit_start=True, sort_keys=False) + "---\n" + body


def _settings() -> dict:
    return {
        "design_doc_patterns": ["docs/requirements/*.md"],
        "impl_file_patterns": ["src/**/*.py"],
        "test_file_patterns": ["tests/**/*.py"],
        "lexicon_file": "project_lexicon.yaml",
    }


def _operation_flow() -> dict:
    return {
        "operations": [
            {
                "id": "configure_delivery",
                "actor": "central_admin",
                "verb": "manage_collection",
                "target": "delivery_target",
                "parent": "course",
                "ui_pattern": "master_detail",
            }
        ]
    }


def test_t08_operation_flow_frontmatter_reaches_node_attributes_and_prompt(tmp_path: Path):
    _write(
        tmp_path / "docs" / "requirements" / "course.md",
        _doc_with_frontmatter({"operation_flow": _operation_flow()}),
    )

    dag = build_dag(tmp_path, _settings())
    node = dag.nodes["docs/requirements/course.md"]

    assert node.attributes["operation_flow"]["operations"][0]["id"] == "configure_delivery"
    hint = operation_flow_hint({"project_root": tmp_path}, [node])
    assert "## Declared operations (operation_flow)" in hint
    assert "configure_delivery" in hint
    assert "master_detail" in hint

    template = tmp_path / "template.md"
    template.write_text("OPS\n{operation_flow_hint}\nEND", encoding="utf-8")
    prompt = build_criteria_expand_prompt(
        task_id="task",
        static_criteria=[],
        design_docs=[node],
        expected_extractions=[],
        project_context={"project_root": tmp_path},
        template_path=template,
    )
    assert "{operation_flow_hint}" not in prompt
    assert "configure_delivery" in prompt


def test_t09_missing_operation_flow_omits_node_attribute_and_hint(tmp_path: Path):
    _write(
        tmp_path / "docs" / "requirements" / "course.md",
        _doc_with_frontmatter({"node_id": "req:course"}),
    )

    dag = build_dag(tmp_path, _settings())
    node = dag.nodes["docs/requirements/course.md"]

    assert "operation_flow" not in node.attributes
    assert operation_flow_hint({"project_root": tmp_path}, [node]) == ""


def test_t10_unknown_ui_pattern_warns_but_is_preserved(tmp_path: Path):
    flow = _operation_flow()
    flow["operations"][0]["ui_pattern"] = "dense_matrix"
    _write(
        tmp_path / "docs" / "requirements" / "course.md",
        _doc_with_frontmatter({"operation_flow": flow}),
    )

    with pytest.warns(UserWarning, match="unknown operation_flow ui_pattern"):
        dag = build_dag(tmp_path, _settings())

    operation = dag.nodes["docs/requirements/course.md"].attributes["operation_flow"]["operations"][0]
    assert operation["ui_pattern"] == "dense_matrix"


def test_operation_flow_hint_is_empty_without_operations():
    node = Node("docs/requirements/course.md", "design_doc", "docs/requirements/course.md", {})

    assert operation_flow_hint({}, [node]) == ""
