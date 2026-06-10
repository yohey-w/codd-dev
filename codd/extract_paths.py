"""Single source of truth for brownfield extract output locations.

`codd extract` writes restored design docs to an isolated output directory so
they never collide with authored sources. Downstream commands (`codd plan`,
`codd restore`, `codd verify`) must read from that SAME location. Historically
the extractor default (``.codd/extract/``) and the planner lookup
(``codd/extracted/``) drifted apart, so brownfield docs written by extract were
invisible to the planner. This module centralizes the path resolution so the two
sides can never diverge again.
"""

from __future__ import annotations

from pathlib import Path

# Default isolated output directory for `codd extract` (Issue #17 isolation).
# Always under the hidden ``.codd`` dir so generated docs never overwrite the
# project's authored ``codd/`` config package or source tree.
EXTRACT_OUTPUT_DIRNAME = ".codd"
EXTRACT_OUTPUT_SUBDIR = "extract"

# Legacy location used by older brownfield projects before output isolation.
# Kept discoverable for backward compatibility.
LEGACY_EXTRACT_OUTPUT_SUBDIR = "extracted"
LEGACY_CODD_DIR_CANDIDATES = ("codd", ".codd")


def default_extract_output_dir(project_root: Path) -> Path:
    """Return the canonical default output dir for `codd extract`.

    This is the single source of truth for where extract writes restored docs.
    """
    return project_root / EXTRACT_OUTPUT_DIRNAME / EXTRACT_OUTPUT_SUBDIR


def extracted_doc_search_dirs(project_root: Path) -> list[Path]:
    """Directories the planner/restore should scan for extracted design docs.

    Order: canonical isolated output first, then legacy ``<codd_dir>/extracted/``
    locations so existing brownfield projects keep working. Only directories that
    exist on disk are returned.
    """
    dirs: list[Path] = []
    canonical = default_extract_output_dir(project_root)
    if canonical.is_dir():
        dirs.append(canonical)
    for name in LEGACY_CODD_DIR_CANDIDATES:
        legacy = project_root / name / LEGACY_EXTRACT_OUTPUT_SUBDIR
        if legacy.is_dir() and legacy not in dirs:
            dirs.append(legacy)
    return dirs
