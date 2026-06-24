"""Unit tests for the shared path-escape jail (:mod:`codd.path_safety`).

The shared utility is pure path logic. These tests pin its contract directly:

* ``../`` parent-traversal, absolute-out-of-root, and in-root-symlink-to-outside
  all resolve to ``None`` (no read) for :func:`resolve_project_path` and
  :func:`project_relative_path`, and are dropped by :func:`iter_project_glob`.
* an in-root path and an in-root symlink whose target stays inside the root are
  ACCEPTED (anti-false-red — a legitimate path must keep working).

The three named per-site jails delegate to this module, so a regression here is a
regression in every unified reader at once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.path_safety import (
    iter_project_glob,
    project_relative_path,
    resolve_project_path,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "feature.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("LEAK\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# resolve_project_path
# ---------------------------------------------------------------------------


def test_resolve_in_root_relative_accepted(project: Path):
    resolved = resolve_project_path(project, "src/feature.py")
    assert resolved is not None
    assert resolved == (project / "src" / "feature.py").resolve()


def test_resolve_parent_traversal_rejected(project: Path):
    assert resolve_project_path(project, "../outside/secret.txt") is None


def test_resolve_absolute_out_of_root_rejected(project: Path):
    outside = project.parent / "outside" / "secret.txt"
    assert resolve_project_path(project, str(outside)) is None


def test_resolve_absolute_in_root_accepted(project: Path):
    inside = project / "src" / "feature.py"
    resolved = resolve_project_path(project, str(inside))
    assert resolved == inside.resolve()


def test_resolve_in_root_symlink_to_outside_rejected(project: Path):
    link = project / "src" / "leak_link.py"
    target = project.parent / "outside" / "secret.txt"
    link.symlink_to(target)
    assert resolve_project_path(project, "src/leak_link.py") is None


def test_resolve_in_root_symlink_to_inside_accepted(project: Path):
    real = project / "src" / "feature.py"
    link = project / "src" / "alias.py"
    link.symlink_to(real)
    resolved = resolve_project_path(project, "src/alias.py")
    assert resolved == real.resolve()


def test_resolve_empty_returns_none(project: Path):
    assert resolve_project_path(project, "") is None
    assert resolve_project_path(project, "   ") is None


# ---------------------------------------------------------------------------
# iter_project_glob
# ---------------------------------------------------------------------------


def test_glob_in_root_match(project: Path):
    matches = iter_project_glob(project, "src/*.py")
    assert (project / "src" / "feature.py").resolve() in matches


def test_glob_leading_slash_is_root_relative(project: Path):
    # An absolute-looking pattern is treated as root-relative (Path.glob rejects
    # absolute patterns); it must NOT escape to the filesystem root.
    matches = iter_project_glob(project, "/src/*.py")
    assert (project / "src" / "feature.py").resolve() in matches


def test_glob_symlink_to_outside_dropped(project: Path):
    link = project / "src" / "leak_link.py"
    target = project.parent / "outside" / "secret.txt"
    link.symlink_to(target)
    matches = iter_project_glob(project, "src/*.py")
    # The symlink entry resolves outside the root and must be excluded.
    assert all("secret.txt" not in str(match) for match in matches)
    assert link.resolve() not in matches


def test_glob_empty_returns_empty(project: Path):
    assert iter_project_glob(project, "") == []


# ---------------------------------------------------------------------------
# project_relative_path
# ---------------------------------------------------------------------------


def test_relative_in_root(project: Path):
    rel = project_relative_path(project, "src/feature.py")
    assert rel == "src/feature.py"


def test_relative_out_of_root_none(project: Path):
    outside = project.parent / "outside" / "secret.txt"
    assert project_relative_path(project, str(outside)) is None
    assert project_relative_path(project, "../outside/secret.txt") is None
