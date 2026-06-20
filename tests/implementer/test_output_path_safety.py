"""Output-path safety for root-module layouts (e.g. Go).

A root-module language profile (package_root.kind=none, module_root=".") legitimately
declares the repo root "." as an output root. Two invariants must hold:
  1. `.` normalizes to a valid in-project path (it is the project root) — while "" and
     ".." traversal stay rejected.
  2. cleaning output paths must NEVER delete the project root itself (a `shutil.rmtree(".")`
     would wipe go.mod, the .codd session, and the whole tree).
"""
from pathlib import Path

import pytest

from codd.implementer import _clean_output_paths, _normalize_project_path


def test_normalize_allows_project_root_dot():
    assert _normalize_project_path(".") == "."
    assert _normalize_project_path("./") == "."


def test_normalize_keeps_normal_paths():
    assert _normalize_project_path("cmd/server/main.go") == "cmd/server/main.go"
    assert _normalize_project_path("go.mod") == "go.mod"


@pytest.mark.parametrize("bad", ["", "..", "../x", "a/../../b"])
def test_normalize_rejects_empty_and_traversal(bad):
    with pytest.raises(ValueError):
        _normalize_project_path(bad)


def test_clean_never_deletes_project_root(tmp_path: Path):
    (tmp_path / "go.mod").write_text("module example.com/app\n")
    (tmp_path / "internal").mkdir()
    (tmp_path / "internal" / "store.go").write_text("package store\n")

    # "." (project root) must be skipped; a real subdir must still be cleaned.
    _clean_output_paths(tmp_path, [".", "internal"])

    assert (tmp_path / "go.mod").exists(), "DANGER: project root was deleted"
    assert tmp_path.exists()
    assert not (tmp_path / "internal").exists()
