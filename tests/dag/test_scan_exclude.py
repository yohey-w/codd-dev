"""``scan.exclude`` honoring tests (cmd_466 #5)."""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import _glob_project_paths, build_dag, load_dag_settings


def test_glob_project_paths_filters_node_modules(tmp_path: Path) -> None:
    (tmp_path / "node_modules" / "zod" / "tests").mkdir(parents=True)
    (tmp_path / "node_modules" / "zod" / "tests" / "a.test.ts").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.ts").write_text("", encoding="utf-8")

    without_exclude = _glob_project_paths(tmp_path, ["**/*.ts"])
    with_exclude = _glob_project_paths(
        tmp_path, ["**/*.ts"], exclude_patterns=["**/node_modules/**"]
    )

    assert any("node_modules" in str(p) for p in without_exclude)
    assert not any("node_modules" in str(p) for p in with_exclude)
    assert any(str(p).endswith("src/real.ts") for p in with_exclude)


def test_glob_project_paths_filters_pycache_and_dist(tmp_path: Path) -> None:
    (tmp_path / "build" / "__pycache__").mkdir(parents=True)
    (tmp_path / "build" / "__pycache__" / "x.py").write_text("", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "out.py").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("", encoding="utf-8")

    result = _glob_project_paths(
        tmp_path,
        ["**/*.py"],
        exclude_patterns=["**/__pycache__/**", "**/dist/**"],
    )
    paths = [str(p) for p in result]
    assert any(p.endswith("src/real.py") for p in paths)
    assert not any("__pycache__" in p for p in paths)
    assert not any("/dist/" in p for p in paths)


def test_load_dag_settings_captures_scan_exclude(tmp_path: Path) -> None:
    settings = load_dag_settings(
        tmp_path,
        {
            "scan": {
                "source_dirs": ["src/"],
                "exclude": ["**/node_modules/**", "**/dist/**"],
            }
        },
    )
    assert "scan_exclude_patterns" in settings
    assert "**/node_modules/**" in settings["scan_exclude_patterns"]
    assert "**/dist/**" in settings["scan_exclude_patterns"]


def test_build_dag_skips_node_modules(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("# impl", encoding="utf-8")
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "fake.py").write_text("# fake", encoding="utf-8")

    dag = build_dag(
        tmp_path,
        {
            "scan": {
                "source_dirs": ["src/", "node_modules/"],
                "exclude": ["**/node_modules/**"],
            },
            "project_type": "generic",
            "impl_file_patterns": ["src/**/*.py", "node_modules/**/*.py"],
        },
    )
    impl_ids = [node.id for node in dag.nodes.values() if node.kind == "impl_file"]
    assert "src/real.py" in impl_ids
    assert not any("node_modules" in node_id for node_id in impl_ids)
