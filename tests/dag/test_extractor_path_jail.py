"""Sink-level path-escape jail for the public readers in ``codd.dag.extractor``.

These adapters consume *user-path-controllable* paths (design-doc / impl /
lexicon paths flowing in from config, CLI args, and DAG node paths). The DAG
builder already jails the paths it feeds in, but these readers are public sinks
reused by other callers, so they take an optional ``project_root`` and apply
defense-in-depth confinement at the sink itself: an external file's
frontmatter / imports / capability evidence / lexicon catalog must never be
consumed when ``project_root`` is supplied.

Fixtures follow the shared convention (``test_path_safety_unified_readers``):
parent-traversal, absolute-outside, in-root-symlink-escape, plus an in-root
regression (anti-false-red). Omitting ``project_root`` preserves legacy
behavior (no jail).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.dag.extractor import (
    extract_design_doc_metadata,
    extract_imports,
    extract_verification_means_catalog,
    scan_capability_evidence,
)


# ---------------------------------------------------------------------------
# extract_design_doc_metadata — frontmatter/body read (fail-closed = raise,
# consistent with its existing loud failure on malformed frontmatter)
# ---------------------------------------------------------------------------


def _outside_design_doc(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = outside / "secret.md"
    doc.write_text("---\ncodd:\n  node_id: leaked\n---\n# secret\n", encoding="utf-8")
    return doc


def test_design_doc_metadata_parent_traversal_with_root_raises(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _outside_design_doc(tmp_path)
    with pytest.raises((ValueError, OSError)):
        extract_design_doc_metadata(
            Path("../outside/secret.md"), project_root=project_root
        )


def test_design_doc_metadata_absolute_outside_with_root_raises(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_design_doc(tmp_path)
    with pytest.raises((ValueError, OSError)):
        extract_design_doc_metadata(doc, project_root=project_root)


def test_design_doc_metadata_symlink_escape_with_root_raises(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_design_doc(tmp_path)
    (project_root / "alias.md").symlink_to(doc)
    with pytest.raises((ValueError, OSError)):
        extract_design_doc_metadata(Path("alias.md"), project_root=project_root)


def test_design_doc_metadata_in_root_with_root_still_read(tmp_path):
    """Anti-false-red: an in-root design doc with project_root reads normally."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "doc.md").write_text(
        "---\ncodd:\n  node_id: real\n  depends_on: docs/x.md\n---\n# real\n",
        encoding="utf-8",
    )
    metadata = extract_design_doc_metadata(
        Path("doc.md"), project_root=project_root
    )
    assert metadata["node_id"] == "real"
    assert metadata["depends_on"] == ["docs/x.md"]


def test_design_doc_metadata_without_root_unchanged(tmp_path):
    """Anti-false-red: omitting project_root preserves legacy behavior."""
    doc = _outside_design_doc(tmp_path)
    metadata = extract_design_doc_metadata(doc)
    assert metadata["node_id"] == "leaked"


# ---------------------------------------------------------------------------
# extract_imports — source-file read (fail-closed = no evidence -> [])
# ---------------------------------------------------------------------------


def _outside_source(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    src = outside / "leak.ts"
    src.write_text("import { x } from './secret';\n", encoding="utf-8")
    return src


def test_extract_imports_parent_traversal_with_root_empty(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _outside_source(tmp_path)
    assert extract_imports(Path("../outside/leak.ts"), project_root=project_root) == []


def test_extract_imports_absolute_outside_with_root_empty(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    src = _outside_source(tmp_path)
    assert extract_imports(src, project_root=project_root) == []


def test_extract_imports_symlink_escape_with_root_empty(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    src = _outside_source(tmp_path)
    (project_root / "alias.ts").symlink_to(src)
    assert extract_imports(Path("alias.ts"), project_root=project_root) == []


def test_extract_imports_in_root_with_root_still_read(tmp_path):
    """Anti-false-red: an in-root source with project_root extracts imports."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "a.ts").write_text("import { x } from './b';\n", encoding="utf-8")
    assert extract_imports(Path("a.ts"), project_root=project_root) == ["./b"]


def test_extract_imports_without_root_unchanged(tmp_path):
    """Anti-false-red: omitting project_root preserves legacy behavior."""
    src = _outside_source(tmp_path)
    assert extract_imports(src) == ["./secret"]


# ---------------------------------------------------------------------------
# scan_capability_evidence — impl-file read (fail-closed = no evidence -> [])
# ---------------------------------------------------------------------------

_PATTERNS = {"writes_db": {"matches": [{"regex": r"db\.save"}]}}


def _outside_impl(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    impl = outside / "leak.py"
    impl.write_text("db.save(x)\n", encoding="utf-8")
    return impl


def test_scan_capability_evidence_parent_traversal_with_root_empty(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _outside_impl(tmp_path)
    assert (
        scan_capability_evidence(
            Path("../outside/leak.py"), _PATTERNS, project_root=project_root
        )
        == []
    )


def test_scan_capability_evidence_absolute_outside_with_root_empty(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    impl = _outside_impl(tmp_path)
    assert scan_capability_evidence(impl, _PATTERNS, project_root=project_root) == []


def test_scan_capability_evidence_symlink_escape_with_root_empty(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    impl = _outside_impl(tmp_path)
    (project_root / "alias.py").symlink_to(impl)
    assert (
        scan_capability_evidence(Path("alias.py"), _PATTERNS, project_root=project_root)
        == []
    )


def test_scan_capability_evidence_in_root_with_root_still_scanned(tmp_path):
    """Anti-false-red: an in-root impl with project_root yields evidence."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "svc.py").write_text("db.save(x)\n", encoding="utf-8")
    evidence = scan_capability_evidence(
        Path("svc.py"), _PATTERNS, project_root=project_root
    )
    assert [e["capability_kind"] for e in evidence] == ["writes_db"]


def test_scan_capability_evidence_without_root_unchanged(tmp_path):
    """Anti-false-red: omitting project_root preserves legacy behavior."""
    impl = _outside_impl(tmp_path)
    evidence = scan_capability_evidence(impl, _PATTERNS)
    assert [e["capability_kind"] for e in evidence] == ["writes_db"]


# ---------------------------------------------------------------------------
# extract_verification_means_catalog — lexicon read (fail-closed = None)
# ---------------------------------------------------------------------------


def _outside_lexicon(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    lex = outside / "lexicon.yaml"
    lex.write_text(
        "verification_means_catalog:\n  m1:\n    description: leaked\n",
        encoding="utf-8",
    )
    return lex


def test_verification_means_catalog_parent_traversal_with_root_none(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _outside_lexicon(tmp_path)
    assert (
        extract_verification_means_catalog(
            Path("../outside/lexicon.yaml"), project_root=project_root
        )
        is None
    )


def test_verification_means_catalog_absolute_outside_with_root_none(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    lex = _outside_lexicon(tmp_path)
    assert extract_verification_means_catalog(lex, project_root=project_root) is None


def test_verification_means_catalog_symlink_escape_with_root_none(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    lex = _outside_lexicon(tmp_path)
    (project_root / "alias.yaml").symlink_to(lex)
    assert (
        extract_verification_means_catalog(
            Path("alias.yaml"), project_root=project_root
        )
        is None
    )


def test_verification_means_catalog_in_root_with_root_still_read(tmp_path):
    """Anti-false-red: an in-root lexicon with project_root returns its catalog."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "lexicon.yaml").write_text(
        "verification_means_catalog:\n  m1:\n    description: real\n",
        encoding="utf-8",
    )
    catalog = extract_verification_means_catalog(
        Path("lexicon.yaml"), project_root=project_root
    )
    assert catalog == {"m1": {"description": "real"}}


def test_verification_means_catalog_without_root_unchanged(tmp_path):
    """Anti-false-red: omitting project_root preserves legacy behavior."""
    lex = _outside_lexicon(tmp_path)
    catalog = extract_verification_means_catalog(lex)
    assert catalog == {"m1": {"description": "leaked"}}
