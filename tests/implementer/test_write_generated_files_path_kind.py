"""``_write_generated_files`` must guard the AI-payload WRITE site against a
path-kind collision, mirroring the v3.34.1 fix in ``_create_output_paths``.

v3.34.1 fixed the SAME file/dir kind bug in ``_create_output_paths`` (which
``mkdir``'d declared outputs), but the ACTUAL write site — where the AI-generated
payload is committed to disk via ``destination.parent.mkdir(...)`` +
``destination.write_text(...)`` — was left unguarded. If the payload's destination
already exists on disk as a DIRECTORY, ``write_text`` raises a raw, unhandled
``IsADirectoryError``; if an ancestor path component is itself a FILE, ``mkdir``
raises a raw ``NotADirectoryError``/``FileExistsError``. Either escapes the
autopilot and crashes the implement stage.

These tests pin the write site to the same honest-red discipline as
``_create_output_paths``: a path-kind collision surfaces as a clean ``StageError``
(routed into the autopilot's clean-red/regenerate path), path-SHAPE/kind only — no
language/framework knowledge.
"""
from pathlib import Path

import pytest

from codd.greenfield.pipeline import StageError
from codd.implementer import DesignContext, ImplementSpec, _write_generated_files


_RAW = (
    "=== FILE: src/errors.js ===\n"
    "```js\nexport class AppError extends Error {}\n```\n"
)


def _spec() -> ImplementSpec:
    return ImplementSpec("design:errors", ["src"], expected_outputs=["src/errors.js"])


def _design_context() -> DesignContext:
    return DesignContext(
        node_id="design:errors",
        path=Path("docs/design/errors.md"),
        content="# Errors\n",
    )


def _write(project_root: Path):
    return _write_generated_files(
        project_root=project_root,
        design_context=_design_context(),
        spec=_spec(),
        dependency_documents=[],
        language="javascript",
        raw_output=_RAW,
        syntax_gate=False,
        confusable_check=False,
    )


def test_destination_that_exists_as_directory_raises_stage_error(tmp_path: Path):
    # Honest-red: the payload's destination path already exists on disk as a
    # DIRECTORY → a raw IsADirectoryError from write_text. Must be a clean StageError.
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "src" / "errors.js").mkdir(parents=True)

    with pytest.raises(StageError, match="path-kind collision"):
        _write(project_root)


def test_file_ancestor_blocking_parent_raises_stage_error(tmp_path: Path):
    # Symmetric honest-red: an ancestor component exists on disk as a FILE, so the
    # parent directory cannot be created → a raw NotADirectoryError/FileExistsError
    # from mkdir. Must be a clean StageError, not a raw OS exception.
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / "src").write_text("i am a file, not a directory\n", encoding="utf-8")

    with pytest.raises(StageError, match="path-kind collision"):
        _write(project_root)


def test_clean_destination_writes_normally(tmp_path: Path):
    # Control: no collision → the payload is written verbatim (the guard is inert on
    # the happy path).
    project_root = tmp_path / "proj"
    project_root.mkdir()

    generated = _write(project_root)

    dest = project_root / "src" / "errors.js"
    assert dest.is_file()
    assert generated == [dest]
    assert "AppError" in dest.read_text(encoding="utf-8")
