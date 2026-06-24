"""Path-escape jail coverage for the propagation family's user-path FS readers.

``codd propagate``/``--verify``/``--commit`` consume several *user-controllable*
filesystem paths and reach a read / ``exists`` / glob / CEG-load sink:

* ``propagator._load_graph`` — ``codd.yaml`` ``graph.path`` (the CEG dir whose
  nodes/edges feed ``_get_doc_confidence``);
* ``propagation_common.iter_design_docs`` — ``codd.yaml`` ``scan.doc_dirs``
  (the design docs surfaced as AffectedDocs by ``_find_design_docs_by_modules``
  and ``_find_docs_depending_on``);
* ``propagator._upstream_fingerprints`` — recorded verify-state upstream paths
  (hashed for the ``--commit`` TOCTOU drift guard);
* ``propagator._find_changed_docs`` — changed-file paths read for frontmatter.

For each reader these tests pin the three escape fixtures the shared
:func:`codd.path_safety.resolve_project_path` jail must reject —

  1. ``../outside`` parent traversal,
  2. an absolute path outside the project root,
  3. an in-root symlink whose target escapes the root

— proving the out-of-root file is neither read nor used as graph/confidence,
affected-doc, fingerprint, or changed-doc evidence, plus an in-root regression
(anti-false-red: a legitimate in-root path still works).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.propagation_common import iter_design_docs
from codd.propagator import (
    _FINGERPRINT_MISSING,
    _find_changed_docs,
    _find_design_docs_by_modules,
    _load_graph,
    _upstream_fingerprints,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text("version: 0.1.0\n", encoding="utf-8")
    return project


def _write_design_doc(path: Path, *, node_id: str, modules: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mods = "[" + ", ".join(modules) + "]"
    path.write_text(
        f"---\ncodd:\n  node_id: {node_id}\n  modules: {mods}\n---\n# {node_id}\n",
        encoding="utf-8",
    )


def _build_graph_dir(graph_dir: Path) -> None:
    """Build a real, loadable CEG (nodes.jsonl + an edge with evidence)."""
    from codd.graph import CEG

    graph_dir.mkdir(parents=True, exist_ok=True)
    graph = CEG(graph_dir)
    graph.upsert_node("design:d", "design", path="docs/d.md")
    graph.upsert_node("module:m", "module")
    eid = graph.add_edge("design:d", "module:m", "depends_on", "technical")
    graph.add_evidence(eid, "frontmatter", "frontmatter", 0.95, "modules field")
    graph.close()


# ---------------------------------------------------------------------------
# _load_graph — codd.yaml graph.path  (fix #1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_load_graph_out_of_root_path_not_loaded(tmp_path, escape):
    project = _make_project(tmp_path)
    outside_graph = tmp_path / "outside_scan"
    _build_graph_dir(outside_graph)

    raw = "../outside_scan" if escape == "parent" else str(outside_graph)
    graph = _load_graph(project, {"graph": {"path": raw}})

    assert graph is None, (
        "graph.path resolving outside the project root was loaded as the CEG"
    )


def test_load_graph_in_root_symlink_escape_not_loaded(tmp_path):
    project = _make_project(tmp_path)
    outside_graph = tmp_path / "outside_scan"
    _build_graph_dir(outside_graph)
    link = project / "scan_link"
    link.symlink_to(outside_graph)

    graph = _load_graph(project, {"graph": {"path": "scan_link"}})

    assert graph is None, (
        "in-root symlink whose target escapes the root was loaded as the CEG"
    )


def test_load_graph_in_root_still_loads(tmp_path):
    """Anti-false-red: an in-root graph dir still loads."""
    project = _make_project(tmp_path)
    _build_graph_dir(project / "codd" / "scan")

    graph = _load_graph(project, {"graph": {"path": "codd/scan"}})

    assert graph is not None, "in-root graph dir must still load"


# ---------------------------------------------------------------------------
# iter_design_docs / _find_design_docs_by_modules — scan.doc_dirs  (fix #2)
# ---------------------------------------------------------------------------


def _seed_outside_doc(tmp_path: Path, modules: list[str]) -> Path:
    outside = tmp_path / "outside"
    _write_design_doc(
        outside / "secret_design.md", node_id="design:secret", modules=modules
    )
    return outside / "secret_design.md"


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_doc_dirs_out_of_root_not_affected(tmp_path, escape):
    project = _make_project(tmp_path)
    secret = _seed_outside_doc(tmp_path, ["m"])
    doc_dir = "../outside" if escape == "parent" else str(secret.parent)

    affected = _find_design_docs_by_modules(
        project, {"scan": {"doc_dirs": [doc_dir]}}, {"m"}, {}
    )

    assert all(a.node_id != "design:secret" for a in affected), (
        "doc_dir outside the project root surfaced an out-of-root affected doc"
    )
    assert all("secret_design" not in a.path for a in affected)


def test_doc_dirs_in_root_symlinked_md_escape_not_affected(tmp_path):
    """rglob follows symlinks: an in-root doc_dir holding a ``*.md`` symlinked to
    an out-of-root design doc must not surface that doc (per-file jail)."""
    project = _make_project(tmp_path)
    secret = _seed_outside_doc(tmp_path, ["m"])
    docs = project / "docs"
    docs.mkdir(parents=True)
    (docs / "leak.md").symlink_to(secret)

    affected = _find_design_docs_by_modules(
        project, {"scan": {"doc_dirs": ["docs"]}}, {"m"}, {}
    )

    assert all(a.node_id != "design:secret" for a in affected), (
        "in-root doc_dir symlinked *.md escaping the root surfaced an affected doc"
    )


def test_doc_dirs_in_root_still_affected(tmp_path):
    """Anti-false-red: an in-root design doc is still affected, and the yielded
    path stays project-relative (callers ``relative_to(project_root)`` it)."""
    project = _make_project(tmp_path)
    _write_design_doc(
        project / "docs" / "real.md", node_id="design:real", modules=["m"]
    )

    docs = list(iter_design_docs(project, {"scan": {"doc_dirs": ["docs"]}}))
    assert any(cd["node_id"] == "design:real" for _p, cd in docs)

    affected = _find_design_docs_by_modules(
        project, {"scan": {"doc_dirs": ["docs"]}}, {"m"}, {}
    )
    assert any(a.node_id == "design:real" and a.path == "docs/real.md" for a in affected), (
        "in-root design doc must still be affected with a project-relative path"
    )


# ---------------------------------------------------------------------------
# _upstream_fingerprints — recorded verify-state upstream paths  (fix #3)
# ---------------------------------------------------------------------------


def _spy_read_bytes(monkeypatch) -> list[Path]:
    seen: list[Path] = []
    real = Path.read_bytes

    def _spy(self, *args, **kwargs):
        seen.append(Path(self).resolve())
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", _spy)
    return seen


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_upstream_fingerprints_out_of_root_not_hashed(tmp_path, monkeypatch, escape):
    project = _make_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("secret upstream\n", encoding="utf-8")

    rel = "../outside/secret.md" if escape == "parent" else str(secret)
    seen = _spy_read_bytes(monkeypatch)
    fps = _upstream_fingerprints(project, [rel])

    assert secret.resolve() not in seen, (
        "out-of-root upstream path was read/hashed for a fingerprint"
    )
    assert fps[rel]["content_hash"] == _FINGERPRINT_MISSING, (
        "out-of-root upstream must fingerprint as missing (so it reads as drift)"
    )


def test_upstream_fingerprints_in_root_symlink_escape_not_hashed(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("secret upstream\n", encoding="utf-8")
    (project / "link.md").symlink_to(secret)

    seen = _spy_read_bytes(monkeypatch)
    fps = _upstream_fingerprints(project, ["link.md"])

    assert secret.resolve() not in seen, (
        "in-root symlink escaping the root was read/hashed for a fingerprint"
    )
    assert fps["link.md"]["content_hash"] == _FINGERPRINT_MISSING


def test_upstream_fingerprints_in_root_still_hashed(tmp_path):
    """Anti-false-red: an in-root upstream doc still gets a real content hash."""
    project = _make_project(tmp_path)
    (project / "up.md").write_text("real upstream\n", encoding="utf-8")

    fps = _upstream_fingerprints(project, ["up.md"])
    assert fps["up.md"]["content_hash"] != _FINGERPRINT_MISSING, (
        "in-root upstream must get a real content hash (anti-false-red)"
    )


# ---------------------------------------------------------------------------
# _find_changed_docs — changed-file path read for frontmatter  (defensive)
# ---------------------------------------------------------------------------


def test_find_changed_docs_in_root_symlink_escape_not_read(tmp_path):
    project = _make_project(tmp_path)
    secret = _seed_outside_doc(tmp_path, ["m"])
    docs = project / "docs"
    docs.mkdir(parents=True)
    (docs / "leak.md").symlink_to(secret)

    result = _find_changed_docs(
        project, {"scan": {"doc_dirs": ["docs"]}}, ["docs/leak.md"]
    )

    assert all(d["node_id"] != "design:secret" for d in result), (
        "in-root changed path symlinked outside the root was read as a changed doc"
    )


def test_find_changed_docs_in_root_still_found(tmp_path):
    """Anti-false-red: an in-root changed design doc is still detected."""
    project = _make_project(tmp_path)
    _write_design_doc(
        project / "docs" / "real.md", node_id="design:real", modules=["m"]
    )

    result = _find_changed_docs(
        project, {"scan": {"doc_dirs": ["docs"]}}, ["docs/real.md"]
    )
    assert any(d["node_id"] == "design:real" for d in result), (
        "in-root changed design doc must still be detected"
    )
