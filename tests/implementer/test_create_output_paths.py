"""``_create_output_paths`` must discriminate file-shaped vs directory-shaped outputs.

Regression: on CoDD v3.34.0 the VB-coverage-closure task became the FIRST task to
declare CONCRETE FILE paths (an owner test file such as
``tests/e2e/helpers/workspace.ts``) in ``ImplementTaskRef.output_paths``.
``_create_output_paths`` blindly ``mkdir``'d every output path AS A DIRECTORY, so it
created those file paths as empty directories; the later ``_write_generated_files``
``destination.write_text(...)`` then raised a raw, unhandled ``IsADirectoryError`` that
escaped the autopilot instead of a clean ``StageError``.

The sibling ``_clean_output_paths`` already discriminates file vs dir; these tests pin
``_create_output_paths`` to the same awareness (path-SHAPE only — no language/framework
knowledge), plus an honest-red guard for a path-kind collision on disk.
"""
from pathlib import Path

import pytest

from codd.greenfield.pipeline import StageError
from codd.implementer import _create_output_paths


def test_file_shaped_output_creates_parent_not_the_file_path(tmp_path: Path):
    # A mix of directory-shaped (``src``, ``tests``) and file-shaped
    # (``tests/e2e/helpers/workspace.ts``) declared outputs — exactly the v3.34.0
    # closure-task shape.
    file_output = "tests/e2e/helpers/workspace.ts"
    _create_output_paths(tmp_path, ["src", "tests", file_output])

    # Directory-shaped outputs are created as directories, as before.
    assert (tmp_path / "src").is_dir()
    assert (tmp_path / "tests").is_dir()

    # The file-shaped output's PARENT is created as a directory ...
    file_dest = tmp_path / file_output
    assert file_dest.parent.is_dir()
    # ... but the file path itself must NOT be created (least of all as a directory).
    assert not file_dest.exists()

    # The regression that reproduced the crash: writing the file now succeeds instead
    # of raising IsADirectoryError (the file path is not an empty directory).
    file_dest.write_text("export const ws = 1;\n", encoding="utf-8")
    assert file_dest.read_text(encoding="utf-8").startswith("export const ws")


def test_file_shaped_output_that_exists_as_directory_raises_stage_error(tmp_path: Path):
    # Honest-red: a file-shaped output already present on disk as a DIRECTORY is a
    # path-kind collision. It must surface as a clean StageError (routed into the
    # autopilot's clean-red/regenerate path), NOT a raw IsADirectoryError.
    file_output = "tests/e2e/helpers/workspace.ts"
    (tmp_path / file_output).mkdir(parents=True)

    with pytest.raises(StageError):
        _create_output_paths(tmp_path, [file_output])


def test_directory_shaped_output_that_exists_as_file_raises_stage_error(tmp_path: Path):
    # Symmetric honest-red: a directory-shaped output already present on disk as a
    # FILE is the mirror-image path-kind collision → clean StageError, not a raw
    # FileExistsError from mkdir.
    (tmp_path / "src").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(StageError):
        _create_output_paths(tmp_path, ["src"])
