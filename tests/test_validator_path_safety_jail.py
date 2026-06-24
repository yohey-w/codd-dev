"""Path-escape jail coverage for the validator's user-path FS readers.

``codd validate`` consumes two *user-controllable* filesystem paths from
``codd.yaml`` and reaches a frontmatter ``read`` / ``exists`` / ``rglob`` or a
``nodes.jsonl`` read sink:

* ``validator._iter_doc_files`` — ``codd.yaml`` ``scan.doc_dirs`` (each ``*.md``
  parsed for CoDD frontmatter, becoming a checked/validated document — i.e.
  evidence);
* ``validator._load_scanned_node_ids`` — ``codd.yaml`` ``graph.path`` (the CEG
  ``nodes.jsonl`` whose node ids seed known convention targets).

For each reader these tests pin the three escape fixtures the shared
:func:`codd.path_safety.resolve_project_path` jail must reject —

  1. ``../outside`` parent traversal,
  2. an absolute path outside the project root,
  3. an in-root symlink whose target escapes the root

— proving the out-of-root file is neither read nor used as a checked document or
scanned node id, plus an in-root regression (anti-false-red: a legitimate
in-root path still works). ``runner.py`` is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.validator import _iter_doc_files, _load_scanned_node_ids


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "codd").mkdir(parents=True)
    return project


def _write_design_doc(path: Path, *, node_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ncodd:\n  node_id: {node_id}\n  type: design\n---\n# {node_id}\n",
        encoding="utf-8",
    )


def _write_nodes_jsonl(path: Path, *, node_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"id": "%s", "type": "design"}\n' % node_id, encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# _iter_doc_files — codd.yaml scan.doc_dirs  (fix #1: configured doc path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_doc_dirs_out_of_root_not_read(tmp_path, escape):
    project = _make_project(tmp_path)
    outside = tmp_path / "outside"
    _write_design_doc(outside / "secret.md", node_id="design:secret")

    doc_dir = "../outside" if escape == "parent" else str(outside)
    docs = list(_iter_doc_files(project, {"scan": {"doc_dirs": [doc_dir]}}))

    assert docs == [], (
        "doc_dir resolving outside the project root surfaced an out-of-root doc"
    )


def test_doc_dirs_in_root_symlinked_md_escape_not_read(tmp_path):
    """rglob follows symlinks: an in-root doc_dir holding a ``*.md`` symlinked to
    an out-of-root design doc must not be yielded (per-file jail)."""
    project = _make_project(tmp_path)
    outside = tmp_path / "outside"
    _write_design_doc(outside / "secret.md", node_id="design:secret")
    docs_dir = project / "docs"
    docs_dir.mkdir(parents=True)
    (docs_dir / "leak.md").symlink_to(outside / "secret.md")

    docs = list(_iter_doc_files(project, {"scan": {"doc_dirs": ["docs"]}}))

    assert all("secret" not in p.name for p in docs), (
        "in-root doc_dir symlinked *.md escaping the root was yielded as a doc"
    )
    assert all(Path(p).resolve() != (outside / "secret.md").resolve() for p in docs)


def test_doc_dirs_in_root_still_read(tmp_path):
    """Anti-false-red: an in-root design doc is still yielded, and stays
    project-relative (callers ``relative_to(project_root)`` it)."""
    project = _make_project(tmp_path)
    _write_design_doc(project / "docs" / "real.md", node_id="design:real")

    docs = list(_iter_doc_files(project, {"scan": {"doc_dirs": ["docs"]}}))

    assert any(p.name == "real.md" for p in docs), (
        "in-root design doc must still be yielded"
    )
    # Must remain relative_to(project_root)-compatible (no crash).
    for p in docs:
        assert p.relative_to(project).as_posix() == "docs/real.md"


# ---------------------------------------------------------------------------
# _load_scanned_node_ids — codd.yaml graph.path  (fix #1: configured graph path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_graph_path_out_of_root_not_read(tmp_path, escape):
    project = _make_project(tmp_path)
    outside_scan = tmp_path / "outside_scan"
    _write_nodes_jsonl(outside_scan / "nodes.jsonl", node_id="design:secret")

    raw = "../outside_scan" if escape == "parent" else str(outside_scan)
    node_ids = _load_scanned_node_ids(project, {"graph": {"path": raw}})

    assert node_ids == set(), (
        "graph.path resolving outside the project root was read for scanned node ids"
    )


def test_graph_path_in_root_symlink_escape_not_read(tmp_path):
    project = _make_project(tmp_path)
    outside_scan = tmp_path / "outside_scan"
    _write_nodes_jsonl(outside_scan / "nodes.jsonl", node_id="design:secret")
    (project / "scan_link").symlink_to(outside_scan)

    node_ids = _load_scanned_node_ids(project, {"graph": {"path": "scan_link"}})

    assert node_ids == set(), (
        "in-root symlink whose target escapes the root was read for scanned node ids"
    )


def test_graph_path_in_root_still_read(tmp_path):
    """Anti-false-red: an in-root scan dir still yields its node ids."""
    project = _make_project(tmp_path)
    _write_nodes_jsonl(project / "codd" / "scan" / "nodes.jsonl", node_id="design:real")

    node_ids = _load_scanned_node_ids(project, {"graph": {"path": "codd/scan"}})

    assert "design:real" in node_ids, "in-root scan dir must still be read"
