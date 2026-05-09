"""Tests for codd assembler — orphan fragment detection."""

from pathlib import Path
import warnings

import pytest
import yaml

from codd.assembler import _collect_generated_fragments
from codd.generator import _load_project_config


def _create_project_with_outputs(tmp_path: Path, *, output_slugs: list[str]) -> Path:
    """Create a minimal project with configured implement output paths."""
    project = tmp_path / "project"
    project.mkdir()

    # codd.yaml
    codd_dir = project / "codd"
    codd_dir.mkdir()
    config = {
        "project": {"name": "demo", "language": "typescript"},
        "scan": {
            "source_dirs": ["src/"],
            "doc_dirs": ["docs/design/"],
        },
        "implement": {
            "default_output_paths": {
                f"docs/design/{slug}.md": [f"src/generated/{slug}"]
                for slug in output_slugs
            }
        },
    }
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )

    return project


def test_collect_fragments_excludes_orphans(tmp_path):
    """Orphan output directories not in configured outputs should be excluded with a warning."""
    project = _create_project_with_outputs(tmp_path, output_slugs=["authentication", "database_foundation"])
    config = _load_project_config(project)

    # Create valid fragment directories (flat layout)
    gen_base = project / "src" / "generated"
    for slug in ["authentication", "database_foundation"]:
        task_dir = gen_base / slug
        task_dir.mkdir(parents=True)
        (task_dir / "index.ts").write_text(f"// {slug}", encoding="utf-8")

    # Create an orphan directory (old task that was renamed)
    orphan_dir = gen_base / "old_removed_task"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "stale.ts").write_text("// should be excluded", encoding="utf-8")

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        fragments = _collect_generated_fragments(project, config)

    # Orphan should trigger a warning
    orphan_warnings = [x for x in w if "old_removed_task" in str(x.message)]
    assert len(orphan_warnings) == 1
    assert "Orphan" in str(orphan_warnings[0].message)

    # Orphan files should NOT be in fragments
    paths = [f["path"] for f in fragments]
    assert not any("old_removed_task" in p for p in paths)

    # Valid fragments should still be collected
    assert any("authentication" in p for p in paths)
    assert any("database_foundation" in p for p in paths)


def test_collect_fragments_no_plan_collects_all(tmp_path):
    """Without an implementation plan, all fragments are collected (no orphan detection)."""
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    config = {
        "project": {"name": "demo", "language": "typescript"},
        "scan": {"source_dirs": ["src/"]},
    }
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )

    # Flat layout
    gen_base = project / "src" / "generated"
    for slug in ["task_a", "task_b"]:
        d = gen_base / slug
        d.mkdir(parents=True)
        (d / "index.ts").write_text(f"// {slug}")

    config_loaded = _load_project_config(project)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        fragments = _collect_generated_fragments(project, config_loaded)

    # No warnings when plan is absent
    orphan_warnings = [x for x in w if "Orphan" in str(x.message)]
    assert len(orphan_warnings) == 0

    # All fragments collected
    assert len(fragments) == 2


def test_collect_fragments_legacy_sprint_dirs(tmp_path):
    """Legacy sprint_N directories are still collected."""
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    config = {
        "project": {"name": "demo", "language": "typescript"},
        "scan": {"source_dirs": ["src/"]},
    }
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )

    # Legacy sprint_N layout
    sprint_dir = project / "src" / "generated" / "sprint_1" / "auth"
    sprint_dir.mkdir(parents=True)
    (sprint_dir / "index.ts").write_text("// legacy auth")

    config_loaded = _load_project_config(project)
    fragments = _collect_generated_fragments(project, config_loaded)

    assert len(fragments) == 1
    assert "sprint_1" in fragments[0]["path"]
