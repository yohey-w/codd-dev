"""Path-escape jail coverage for readers unified onto :mod:`codd.path_safety`.

Each reader here consumes a *user-controllable* path (codd.yaml config value or a
CLI arg) and reaches a filesystem read/exists/glob sink. These tests pin, per
reader, the three escape fixtures the shared jail must reject —

  1. ``../outside`` parent traversal
  2. an absolute path outside the project root
  3. an in-root symlink whose target escapes the root

— proving the external file is neither read nor used as a PASS witness, plus an
in-root regression (anti-false-red: a legitimate in-root path still works).

Covers the newly-jailed/hardened sinks (the three named readers and the
config_fs closure sites already have dedicated suites in
``test_config_fs_path_root_jail.py`` and ``dag/test_implementation_coverage_path_matcher.py``):

* ``propagator._find_design_docs_by_modules`` — ``wave_config[*].output``
* ``screen_transition_extractor.extract_transitions`` — ``src_dirs`` arg / config
* ``e2e_extractor._configured_doc_files`` — ``scan.doc_dirs`` (per-file symlink gap)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.e2e_extractor import _configured_doc_files
from codd.propagator import _find_design_docs_by_modules
from codd.screen_transition_extractor import extract_transitions


# ---------------------------------------------------------------------------
# propagator._find_design_docs_by_modules — wave_config[*].output
# ---------------------------------------------------------------------------


def _wave_config(output: str) -> dict:
    return {
        "wave_config": {
            "wave1": [
                {"node_id": "leaked", "modules": ["m"], "output": output, "title": "leak"}
            ]
        }
    }


def _outside_doc(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = outside / "secret_design.md"
    doc.write_text("---\ncodd:\n  modules: [m]\n---\n# secret\n", encoding="utf-8")
    return doc


@pytest.mark.parametrize("make_output", ["parent", "absolute"])
def test_wave_config_output_outside_root_not_affected(tmp_path, make_output):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_doc(tmp_path)
    raw = "../outside/secret_design.md" if make_output == "parent" else str(doc)

    affected = _find_design_docs_by_modules(
        project_root, _wave_config(raw), {"m"}, {}
    )
    assert all(a.node_id != "leaked" for a in affected), (
        "wave_config output outside project root was treated as affected"
    )
    assert all("secret_design" not in a.path for a in affected)


def test_wave_config_output_in_root_symlink_escape_not_affected(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_doc(tmp_path)
    link = project_root / "alias.md"
    link.symlink_to(doc)

    affected = _find_design_docs_by_modules(
        project_root, _wave_config("alias.md"), {"m"}, {}
    )
    assert all(a.node_id != "leaked" for a in affected), (
        "in-root symlink escaping the root was treated as affected"
    )


def test_wave_config_output_in_root_still_affected(tmp_path):
    """Anti-false-red: an in-root wave_config output is still affected."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "doc.md").write_text("# d\n", encoding="utf-8")

    affected = _find_design_docs_by_modules(
        project_root, _wave_config("doc.md"), {"m"}, {}
    )
    assert any(a.node_id == "leaked" and a.path == "doc.md" for a in affected), (
        "in-root wave_config output must still be reported as affected"
    )


# ---------------------------------------------------------------------------
# screen_transition_extractor.extract_transitions — src_dirs arg / config
# ---------------------------------------------------------------------------

_TRANSITION_SRC = "import { useRouter } from 'next/router';\n"


def _seed_outside_source(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.tsx").write_text(_TRANSITION_SRC, encoding="utf-8")
    return outside


def test_screen_transition_src_dirs_parent_traversal_not_walked(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _seed_outside_source(tmp_path)

    read_paths: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self, *args, **kwargs):
        read_paths.append(Path(self).resolve())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy)
    extract_transitions(project_root, src_dirs=["../outside"])
    assert all("leak.tsx" not in str(p) for p in read_paths), (
        "src_dirs parent-traversal walked/read an out-of-root source file"
    )


def test_screen_transition_src_dirs_absolute_outside_not_walked(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = _seed_outside_source(tmp_path)

    read_paths: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self, *args, **kwargs):
        read_paths.append(Path(self).resolve())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy)
    extract_transitions(project_root, src_dirs=[str(outside)])
    assert all("leak.tsx" not in str(p) for p in read_paths), (
        "absolute out-of-root src_dir walked/read an external source file"
    )


def test_screen_transition_src_dirs_symlink_escape_not_walked(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = _seed_outside_source(tmp_path)
    link = project_root / "linked_src"
    link.symlink_to(outside)

    read_paths: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self, *args, **kwargs):
        read_paths.append(Path(self).resolve())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy)
    extract_transitions(project_root, src_dirs=["linked_src"])
    assert all("leak.tsx" not in str(p) for p in read_paths), (
        "in-root symlink to an outside dir walked/read an external source file"
    )


# ---------------------------------------------------------------------------
# e2e_extractor._configured_doc_files — scan.doc_dirs (per-file symlink gap)
# ---------------------------------------------------------------------------


def test_doc_dirs_per_file_symlink_escape_dropped(tmp_path):
    """An in-root doc-dir containing a symlink to an outside file must not
    enumerate that file (per-file symlink jail inside the rglob)."""
    project_root = tmp_path / "project"
    docs = project_root / "docs"
    docs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("# secret\n", encoding="utf-8")
    (docs / "leak.md").symlink_to(secret)
    # A real in-root doc so the directory itself resolves/enumerates.
    (docs / "real.md").write_text("# real\n", encoding="utf-8")

    files = _configured_doc_files(project_root, {"scan": {"doc_dirs": ["docs/"]}})
    names = {p.name for p in files}
    resolved = {p.resolve() for p in files}
    assert secret.resolve() not in resolved, (
        "doc_dir symlink escaping the root was enumerated"
    )
    assert "real.md" in names, "in-root doc must still be enumerated (anti-false-red)"


def test_doc_dirs_absolute_outside_not_enumerated(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# secret\n", encoding="utf-8")

    files = _configured_doc_files(project_root, {"scan": {"doc_dirs": [str(outside)]}})
    assert all("secret" not in p.name for p in files), (
        "absolute out-of-root doc_dir was enumerated"
    )
