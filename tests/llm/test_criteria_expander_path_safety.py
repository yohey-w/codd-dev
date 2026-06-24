"""Path-escape jail tests for criteria_expander (round-9).

The criteria expander reads several *user-path-controllable* filesystem inputs as
project evidence: design-doc node paths, ``--design-doc`` / ``--expected-extraction``
paths, the task YAML path, and the lexicon path (codd.yaml ``lexicon_path`` /
context). When such a path is absolute-out-of-root, a ``../`` traversal, or an
in-root symlink whose target escapes the tree, the read must be refused (the file
must not be read or consumed as evidence) — a path-escape false-green otherwise.

These tests pin: out-of-root → NOT read / NOT evidence (skip, never crash);
in-root → unchanged (anti-false-red regression).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.dag import Node
from codd.llm.criteria_expander import (
    _node_content,
    coverage_axes_hint,
    find_task_yaml,
    load_design_docs,
    load_expected_extractions,
)


def _symlink_or_skip(link: Path, target: Path, *, dir_target: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=dir_target)
    except (OSError, NotImplementedError):  # pragma: no cover - platform guard
        pytest.skip("symlinks unsupported on this platform")


# --- _node_content (design-doc evidence read) ----------------------------------


def test_node_content_absolute_path_outside_root_is_not_read(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("OUT OF ROOT SECRET", encoding="utf-8")

    node = Node(id="x", kind="design_doc", path=str(secret), attributes={})

    assert _node_content(node, project_root) == ""


def test_node_content_parent_traversal_path_is_not_read(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("OUT OF ROOT SECRET", encoding="utf-8")

    node = Node(id="x", kind="design_doc", path="../secret.md", attributes={})

    assert _node_content(node, project_root) == ""


def test_node_content_symlink_escaping_root_is_not_read(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("OUT OF ROOT SECRET", encoding="utf-8")
    link = project_root / "docs.md"
    _symlink_or_skip(link, secret)

    node = Node(id="x", kind="design_doc", path="docs.md", attributes={})

    assert _node_content(node, project_root) == ""


def test_node_content_in_root_relative_path_is_read_unchanged(tmp_path):
    project_root = tmp_path / "project"
    doc = project_root / "docs" / "design" / "spec.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("IN ROOT BODY", encoding="utf-8")

    node = Node(id="x", kind="design_doc", path="docs/design/spec.md", attributes={})

    assert _node_content(node, project_root) == "IN ROOT BODY"


def test_node_content_inline_content_attr_is_preserved(tmp_path):
    node = Node(id="x", kind="design_doc", path="anything.md", attributes={"content": "INLINE"})

    assert _node_content(node, tmp_path) == "INLINE"


# --- load_design_docs ----------------------------------------------------------


def test_load_design_docs_absolute_path_outside_root_is_excluded(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("OUT OF ROOT", encoding="utf-8")

    nodes = load_design_docs(project_root, [secret])

    assert nodes == []


def test_load_design_docs_parent_traversal_is_excluded(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("OUT OF ROOT", encoding="utf-8")

    nodes = load_design_docs(project_root, ["../secret.md"])

    assert nodes == []


def test_load_design_docs_symlink_escaping_root_is_excluded(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("OUT OF ROOT", encoding="utf-8")
    link = project_root / "spec.md"
    _symlink_or_skip(link, secret)

    nodes = load_design_docs(project_root, ["spec.md"])

    assert nodes == []


def test_load_design_docs_in_root_path_unchanged(tmp_path):
    project_root = tmp_path / "project"
    doc = project_root / "docs" / "design" / "spec.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("IN ROOT", encoding="utf-8")

    nodes = load_design_docs(project_root, [doc])

    assert len(nodes) == 1
    assert nodes[0].attributes["content"] == "IN ROOT"
    assert nodes[0].path == "docs/design/spec.md"


# --- load_expected_extractions -------------------------------------------------


def test_load_expected_extractions_absolute_outside_root_is_excluded(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    secret = tmp_path / "expected.yaml"
    secret.write_text(yaml.safe_dump({"expected_nodes": [{"path_hint": "src/a.py"}]}), encoding="utf-8")

    loaded = load_expected_extractions([secret], project_root=project_root)

    assert loaded == []


def test_load_expected_extractions_in_root_path_unchanged(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    path = project_root / "expected.yaml"
    path.write_text(yaml.safe_dump({"expected_nodes": [{"path_hint": "src/a.py"}]}), encoding="utf-8")

    loaded = load_expected_extractions([path], project_root=project_root)

    assert loaded[0]["expected_nodes"][0]["path_hint"] == "src/a.py"


# --- find_task_yaml ------------------------------------------------------------


def test_find_task_yaml_absolute_outside_root_is_rejected(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside_task.yaml"
    outside.write_text(yaml.safe_dump({"task_id": "t"}), encoding="utf-8")

    assert find_task_yaml(project_root, str(outside)) is None


def test_find_task_yaml_in_root_explicit_path_unchanged(tmp_path):
    project_root = tmp_path / "project"
    task_dir = project_root / ".codd" / "tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "task_a.yaml"
    task_path.write_text(yaml.safe_dump({"task_id": "task_a"}), encoding="utf-8")

    assert find_task_yaml(project_root, str(task_path)) == task_path.resolve()


def test_find_task_yaml_in_root_by_task_id_unchanged(tmp_path):
    project_root = tmp_path / "project"
    task_dir = project_root / "queue" / "tasks"
    task_dir.mkdir(parents=True)
    task_path = task_dir / "worker.yaml"
    task_path.write_text(yaml.safe_dump({"task_id": "task_c"}), encoding="utf-8")

    assert find_task_yaml(project_root, "task_c") == task_path.resolve()


# --- lexicon (coverage_axes) ---------------------------------------------------


def test_coverage_axes_hint_lexicon_path_outside_root_is_not_read(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    secret = tmp_path / "evil_lexicon.yaml"
    secret.write_text(
        yaml.safe_dump({"coverage_axes": [{"axis_type": "leaked_axis", "variants": [{"id": "x"}]}]}),
        encoding="utf-8",
    )

    hint = coverage_axes_hint(
        {"project_root": project_root, "lexicon_path": str(secret)}, []
    )

    assert "leaked_axis" not in hint


def test_coverage_axes_hint_in_root_lexicon_unchanged(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "project_lexicon.yaml").write_text(
        yaml.safe_dump({"coverage_axes": [{"axis_type": "display_surface", "variants": [{"id": "compact"}]}]}),
        encoding="utf-8",
    )

    hint = coverage_axes_hint({"project_root": project_root}, [])

    assert "display_surface" in hint
    assert "compact" in hint
