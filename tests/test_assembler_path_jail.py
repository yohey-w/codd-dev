"""Red-before-green: the assembler must jail AI-chosen file paths and never
partially apply.

``_write_assembled_files`` writes ``project_root / file_path_str`` where
``file_path_str`` comes verbatim from AI ``=== FILE: path ===`` blocks. Without
a jail, ``project_root / "/abs"`` yields ``/abs`` (absolute escape), ``../``
escapes the tree, and an in-root symlink can redirect a write outside the tree.
Because writes happen per-iteration, a later out-of-jail block also leaves the
earlier (valid) files on disk — a partial apply.

Every target must be resolved and confined to ``project_root`` in a preflight
pass, BEFORE any byte is written, so an escaping block writes nothing at all.
Root-level manifest files (``package.json`` etc.) stay allowed.
"""

from __future__ import annotations

import pytest

from codd.assembler import _write_assembled_files


def _project(tmp_path):
    project_root = tmp_path / "proj"
    (project_root / "src").mkdir(parents=True)
    return project_root, project_root / "src"


def test_write_allows_in_root_and_root_manifest(tmp_path):
    """Sanity: legitimate in-root paths (nested + root-level manifest) still
    write — the jail must not over-reject."""
    project_root, dest = _project(tmp_path)
    raw_output = (
        "=== FILE: package.json ===\n"
        '{"name": "x"}\n'
        "=== FILE: src/app/main.py ===\n"
        "print('hi')\n"
    )
    written = _write_assembled_files(project_root, dest, raw_output)
    assert written == 2
    assert (project_root / "package.json").exists()
    assert (project_root / "src" / "app" / "main.py").exists()


def test_write_rejects_absolute_path_escape(tmp_path):
    """An absolute AI-chosen path must not write outside project_root."""
    project_root, dest = _project(tmp_path)
    outside = tmp_path / "outside_evil.txt"
    raw_output = f"=== FILE: {outside} ===\nPWNED\n"

    with pytest.raises(Exception):
        _write_assembled_files(project_root, dest, raw_output)
    assert not outside.exists()


def test_write_rejects_parent_traversal_escape(tmp_path):
    """A ``../`` AI-chosen path must not write outside project_root."""
    project_root, dest = _project(tmp_path)
    raw_output = "=== FILE: ../escaped.txt ===\nPWNED\n"

    with pytest.raises(Exception):
        _write_assembled_files(project_root, dest, raw_output)
    assert not (tmp_path / "escaped.txt").exists()


def test_write_rejects_symlink_escape(tmp_path):
    """An in-root symlink whose target escapes the tree must not be followed."""
    project_root, dest = _project(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (project_root / "link").symlink_to(outside_dir)
    raw_output = "=== FILE: link/evil.txt ===\nPWNED\n"

    with pytest.raises(Exception):
        _write_assembled_files(project_root, dest, raw_output)
    assert not (outside_dir / "evil.txt").exists()


def test_no_partial_apply_when_later_block_escapes(tmp_path):
    """A valid block followed by an out-of-jail block must write NOTHING —
    no partial apply."""
    project_root, dest = _project(tmp_path)
    evil = tmp_path / "evil_marker.txt"
    raw_output = (
        "=== FILE: src/good.txt ===\n"
        "GOOD\n"
        f"=== FILE: {evil} ===\n"
        "EVIL\n"
    )

    with pytest.raises(Exception):
        _write_assembled_files(project_root, dest, raw_output)
    assert not (project_root / "src" / "good.txt").exists()
    assert not evil.exists()
