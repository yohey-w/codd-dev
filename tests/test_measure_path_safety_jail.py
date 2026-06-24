"""Path-escape jail coverage for ``codd measure`` coverage-metric walks.

``_collect_coverage_metrics`` walks user-controllable ``scan.source_dirs`` and
``scan.doc_dirs`` (codd.yaml) and folds the result into coverage evidence:

* source files under ``source_dirs`` are counted as ``source_files`` (the
  coverage denominator),
* ``*.md`` under ``doc_dirs`` are counted as ``design_documents`` and their
  frontmatter ``source_refs`` become ``tracked_files`` (the numerator).

A configured dir that is absolute, ``../`` traversal, or an in-root symlink
whose target escapes the project root must NOT be walked — otherwise an
out-of-root tree inflates the metrics with files that are not in-project
evidence. These tests pin the three escape fixtures the shared
:func:`codd.path_safety.resolve_project_path` jail must reject, plus in-root
regressions (anti-false-red). Escapes are *excluded* (skipped, with a stderr
diagnostic) rather than crashed on. ``runner.py`` is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.measure import run_measure


def _make_project(tmp_path: Path, config: dict) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return project


def _seed_outside_tree(tmp_path: Path) -> Path:
    """An out-of-root dir holding a source file and a design doc with source_refs."""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "leak_a.py").write_text("x = 1\n", encoding="utf-8")
    (outside / "leak_b.py").write_text("y = 2\n", encoding="utf-8")
    (outside / "leak_doc.md").write_text(
        "---\nsource_refs:\n  - leaked/ref_one.py\n  - leaked/ref_two.py\n---\n# leak\n",
        encoding="utf-8",
    )
    return outside


# --- source_dirs escape -------------------------------------------------------


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_source_dir_out_of_root_not_counted(tmp_path, escape):
    outside = _seed_outside_tree(tmp_path)
    raw = "../outside" if escape == "parent" else str(outside)
    project = _make_project(
        tmp_path,
        {"scan": {"source_dirs": [raw], "doc_dirs": []}, "policies": []},
    )

    result = run_measure(project)

    assert result.coverage.source_files == 0, (
        "out-of-root source dir inflated the coverage source-file count"
    )


def test_source_dir_in_root_symlink_escape_not_counted(tmp_path):
    outside = _seed_outside_tree(tmp_path)
    project = _make_project(
        tmp_path,
        {"scan": {"source_dirs": ["linked_src"], "doc_dirs": []}, "policies": []},
    )
    (project / "linked_src").symlink_to(outside, target_is_directory=True)

    result = run_measure(project)

    assert result.coverage.source_files == 0, (
        "in-root symlink escaping the root inflated the coverage source-file count"
    )


# --- doc_dirs escape ----------------------------------------------------------


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_doc_dir_out_of_root_not_counted(tmp_path, escape):
    outside = _seed_outside_tree(tmp_path)
    raw = "../outside" if escape == "parent" else str(outside)
    project = _make_project(
        tmp_path,
        {"scan": {"source_dirs": [], "doc_dirs": [raw]}, "policies": []},
    )

    result = run_measure(project)

    assert result.coverage.design_documents == 0, (
        "out-of-root design doc was counted in design_documents"
    )
    assert result.coverage.tracked_files == 0, (
        "out-of-root design doc's source_refs leaked into tracked_files evidence"
    )


def test_doc_dir_in_root_symlink_escape_not_counted(tmp_path):
    outside = _seed_outside_tree(tmp_path)
    project = _make_project(
        tmp_path,
        {"scan": {"source_dirs": [], "doc_dirs": ["linked_docs"]}, "policies": []},
    )
    (project / "linked_docs").symlink_to(outside, target_is_directory=True)

    result = run_measure(project)

    assert result.coverage.design_documents == 0
    assert result.coverage.tracked_files == 0, (
        "in-root symlink escaping the root leaked source_refs into tracked_files"
    )


# --- anti-false-red: in-root evidence is unchanged ----------------------------


def test_in_root_source_and_docs_still_counted(tmp_path):
    # Non-default dir names ("mysrc"/"mydocs") avoid load_project_config's default
    # injection (it appends "src/"/"docs/"), which would otherwise double-list a
    # "docs" entry — unrelated to the jail under test.
    project = _make_project(
        tmp_path,
        {"scan": {"source_dirs": ["mysrc"], "doc_dirs": ["mydocs"]}, "policies": []},
    )
    src = project / "mysrc"
    src.mkdir()
    (src / "main.py").write_text("import os\n", encoding="utf-8")
    (src / "util.py").write_text("x = 1\n", encoding="utf-8")
    docs = project / "mydocs"
    docs.mkdir()
    (docs / "design.md").write_text(
        "---\nsource_refs:\n  - mysrc/main.py\n---\n# design\n", encoding="utf-8"
    )

    result = run_measure(project)

    assert result.coverage.source_files == 2
    assert result.coverage.design_documents == 1
    assert result.coverage.tracked_files == 1


def test_in_root_alongside_escape_counts_only_in_root(tmp_path):
    """A mix of in-root and out-of-root dirs counts the in-root one only."""
    outside = _seed_outside_tree(tmp_path)
    project = _make_project(
        tmp_path,
        {
            # "mysrc" avoids load_project_config's default "src/" injection that
            # would double-count the in-root dir (unrelated to the jail).
            "scan": {"source_dirs": ["mysrc", str(outside)], "doc_dirs": []},
            "policies": [],
        },
    )
    src = project / "mysrc"
    src.mkdir()
    (src / "only.py").write_text("x = 1\n", encoding="utf-8")

    result = run_measure(project)

    assert result.coverage.source_files == 1, (
        "escape dir leaked into the count alongside the in-root dir"
    )


def test_source_dir_escape_emits_visibility_warning(tmp_path, capsys):
    """Visibility: an excluded out-of-root config dir is reported, not silently
    swallowed (the count stays 0, and the exclusion surfaces on stderr)."""
    outside = _seed_outside_tree(tmp_path)
    project = _make_project(
        tmp_path,
        {"scan": {"source_dirs": [str(outside)], "doc_dirs": []}, "policies": []},
    )

    run_measure(project)

    err = capsys.readouterr().err
    assert "outside the project root" in err
    assert "excluded" in err
