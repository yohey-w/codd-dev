"""Common node type (cmd_467 v2.15.0)."""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import (
    _common_node_patterns,
    _design_doc_declares_common,
    build_dag,
    load_dag_settings,
)
from codd.dag.checks.transitive_closure import TransitiveClosureCheck


def _seed_project(root: Path, *, with_design: bool = True) -> None:
    if with_design:
        (root / "docs" / "design").mkdir(parents=True)
        (root / "docs" / "design" / "spec.md").write_text(
            "---\ncodd:\n  node_id: design:spec\n  type: design\n---\n# spec\n",
            encoding="utf-8",
        )
    (root / "src" / "lib").mkdir(parents=True)
    (root / "src" / "lib" / "prisma.ts").write_text("export const db = {};", encoding="utf-8")
    (root / "src" / "feature").mkdir()
    (root / "src" / "feature" / "module.ts").write_text("// feature impl", encoding="utf-8")


def test_design_doc_declares_common_via_codd_block():
    assert _design_doc_declares_common(
        {"codd": {"node_type": "common"}}, {}
    ) is True


def test_design_doc_declares_common_via_top_level_node_type():
    assert _design_doc_declares_common({"node_type": "common"}, {}) is True


def test_design_doc_declares_common_falls_back_to_design_when_absent():
    assert _design_doc_declares_common({"codd": {"node_id": "x"}}, {}) is False
    assert _design_doc_declares_common(None, None) is False


def test_common_node_patterns_helper_returns_empty_when_unset():
    assert _common_node_patterns({}) == []
    assert _common_node_patterns({"common_node_patterns": "string-not-list"}) == []


def test_common_node_patterns_helper_expands_braces():
    patterns = _common_node_patterns(
        {"common_node_patterns": ["src/{lib,utils}/**/*.ts"]}
    )
    assert "src/lib/**/*.ts" in patterns
    assert "src/utils/**/*.ts" in patterns


def test_load_dag_settings_captures_top_level_common_patterns(tmp_path: Path) -> None:
    settings = load_dag_settings(
        tmp_path,
        {
            "scan": {"source_dirs": ["src/"]},
            "common_node_patterns": ["src/lib/**/*.ts"],
            "project_type": "generic",
        },
    )
    assert "src/lib/**/*.ts" in settings["common_node_patterns"]


def test_load_dag_settings_also_captures_scan_nested_common_patterns(tmp_path: Path) -> None:
    settings = load_dag_settings(
        tmp_path,
        {
            "scan": {
                "source_dirs": ["src/"],
                "common_node_patterns": ["src/middleware.ts"],
            },
            "project_type": "generic",
        },
    )
    assert "src/middleware.ts" in settings["common_node_patterns"]


def test_build_dag_marks_matching_files_as_common(tmp_path: Path) -> None:
    _seed_project(tmp_path, with_design=False)
    dag = build_dag(
        tmp_path,
        {
            "scan": {"source_dirs": ["src/"]},
            "common_node_patterns": ["src/lib/**/*.ts"],
            "project_type": "generic",
            "impl_file_patterns": ["src/**/*.ts"],
        },
    )
    kinds = {node.id: node.kind for node in dag.nodes.values()}
    assert kinds["src/lib/prisma.ts"] == "common"
    assert kinds["src/feature/module.ts"] == "impl_file"


def test_common_nodes_excluded_from_transitive_closure_unreachable(tmp_path: Path) -> None:
    _seed_project(tmp_path)
    dag = build_dag(
        tmp_path,
        {
            "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/design/"]},
            "common_node_patterns": ["src/lib/**/*.ts"],
            "project_type": "generic",
            "impl_file_patterns": ["src/**/*.ts"],
            "design_doc_patterns": ["docs/design/*.md"],
        },
    )

    result = TransitiveClosureCheck(dag=dag).run()
    # The library file is common and must not be flagged as unreachable.
    assert "src/lib/prisma.ts" not in result.unreachable_nodes
    # The non-common impl file with no parent design_doc still surfaces as
    # unreachable (the legacy behaviour for impl_file).
    assert "src/feature/module.ts" in result.unreachable_nodes
    assert result.common_node_count >= 1


def test_design_doc_frontmatter_can_opt_into_common(tmp_path: Path) -> None:
    (tmp_path / "docs" / "design").mkdir(parents=True)
    (tmp_path / "docs" / "design" / "shared_infra.md").write_text(
        "---\ncodd:\n  node_id: design:shared_infra\n  node_type: common\n  type: design\n---\n# shared\n",
        encoding="utf-8",
    )
    dag = build_dag(
        tmp_path,
        {
            "scan": {"doc_dirs": ["docs/design/"]},
            "project_type": "generic",
            "design_doc_patterns": ["docs/design/*.md"],
        },
    )

    node = dag.nodes["docs/design/shared_infra.md"]
    assert node.kind == "common"


def test_common_node_patterns_empty_falls_back_to_impl(tmp_path: Path) -> None:
    _seed_project(tmp_path, with_design=False)
    dag = build_dag(
        tmp_path,
        {
            "scan": {"source_dirs": ["src/"]},
            "project_type": "generic",
            "impl_file_patterns": ["src/**/*.ts"],
        },
    )
    kinds = {node.id: node.kind for node in dag.nodes.values()}
    # No common patterns declared, so library files remain impl_file (legacy behaviour).
    assert kinds["src/lib/prisma.ts"] == "impl_file"
