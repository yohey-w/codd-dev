"""Path-escape jail coverage for the four coverage/audit-layer readers.

Each reader here consumes a *user-controllable* path (a ``codd.yaml`` config
value) and reaches a filesystem read / ``is_file`` / ``rglob`` sink that either
reads the file as **test evidence** or credits its existence as a **PASS
witness**. An absolute path, a ``../`` parent traversal, or an in-root symlink
whose target escapes the project root must NOT be read or credited — otherwise an
out-of-root file becomes a path-escape false-green.

Sinks pinned here (round-9 confirmed fixes):

* ``operational_e2e_audit._iter_test_files`` — ``scan.test_dirs`` (test evidence)
* ``verifiable_behavior_audit.discover_vb_documents`` — ``test_coverage.docs``
* ``coverage_execution_coherence._test_case_keys_by_vb`` — reuses
  ``_iter_test_files`` (``scan.test_dirs``) to read covering test files
* ``coverage_auditor._discover_existing_artifacts`` — ``artifact_discovery.paths``
  and ``artifact_paths`` overrides (artifact-existence PASS credit)

Each block pins the three escape fixtures (parent traversal, absolute-outside,
in-root symlink escape) plus an in-root regression (anti-false-red: a legitimate
in-root path is still read / credited).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.coverage_auditor import CoverageAuditor
from codd.operational_e2e_audit import _iter_test_files
from codd.verifiable_behavior_audit import discover_vb_documents


# ---------------------------------------------------------------------------
# operational_e2e_audit._iter_test_files — scan.test_dirs (test evidence)
# ---------------------------------------------------------------------------


def _seed_outside_test(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    leak = outside / "leak.test.ts"
    leak.write_text(
        "// codd: covers operation=leaked axis=happy_path\n", encoding="utf-8"
    )
    return leak


def _seed_in_root_test(project_root: Path) -> Path:
    tests = project_root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    real = tests / "real.test.ts"
    real.write_text(
        "// codd: covers operation=real axis=happy_path\n", encoding="utf-8"
    )
    return real


@pytest.mark.parametrize("kind", ["parent", "absolute"])
def test_iter_test_files_outside_root_not_yielded(tmp_path, kind):
    project_root = tmp_path / "project"
    project_root.mkdir()
    leak = _seed_outside_test(tmp_path)
    raw = "../outside" if kind == "parent" else str(leak.parent)

    yielded = list(_iter_test_files(project_root, test_dirs=[raw]))
    resolved = {p.resolve() for p in yielded}
    assert leak.resolve() not in resolved, (
        f"{kind} out-of-root test_dir yielded an external test file as evidence"
    )


def test_iter_test_files_in_root_symlink_escape_not_yielded(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    leak = _seed_outside_test(tmp_path)
    link = project_root / "linked_tests"
    link.symlink_to(leak.parent)

    yielded = list(_iter_test_files(project_root, test_dirs=["linked_tests"]))
    resolved = {p.resolve() for p in yielded}
    assert leak.resolve() not in resolved, (
        "in-root symlink escaping the root yielded an external test file"
    )


def test_iter_test_files_in_root_still_yielded(tmp_path):
    """Anti-false-red: an in-root test dir still yields its test files."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    real = _seed_in_root_test(project_root)

    yielded = list(_iter_test_files(project_root, test_dirs=["tests"]))
    resolved = {p.resolve() for p in yielded}
    assert real.resolve() in resolved, (
        "in-root test file must still be yielded as evidence (anti-false-red)"
    )


# ---------------------------------------------------------------------------
# verifiable_behavior_audit.discover_vb_documents — test_coverage.docs
# ---------------------------------------------------------------------------


def _seed_outside_vb_doc(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = outside / "secret_vb.md"
    doc.write_text(
        "| VB-LEAK | leaked behaviour | tests/x |\n", encoding="utf-8"
    )
    return doc


def _config_docs(raw: str) -> dict:
    return {"test_coverage": {"docs": [raw]}}


@pytest.mark.parametrize("kind", ["parent", "absolute"])
def test_discover_vb_documents_outside_root_not_returned(tmp_path, kind):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _seed_outside_vb_doc(tmp_path)
    raw = "../outside/secret_vb.md" if kind == "parent" else str(doc)

    docs = discover_vb_documents(project_root, config=_config_docs(raw))
    resolved = {p.resolve() for p in docs}
    assert doc.resolve() not in resolved, (
        f"{kind} out-of-root test_coverage.docs entry returned an external VB doc"
    )


def test_discover_vb_documents_in_root_symlink_escape_not_returned(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _seed_outside_vb_doc(tmp_path)
    (project_root / "alias.md").symlink_to(doc)

    docs = discover_vb_documents(project_root, config=_config_docs("alias.md"))
    resolved = {p.resolve() for p in docs}
    assert doc.resolve() not in resolved, (
        "in-root symlink escaping the root returned an external VB doc"
    )


def test_discover_vb_documents_in_root_still_returned(tmp_path):
    """Anti-false-red: an in-root VB doc is still discovered."""
    project_root = tmp_path / "project"
    docs_dir = project_root / "docs" / "test"
    docs_dir.mkdir(parents=True)
    real = docs_dir / "vb.md"
    real.write_text("| VB-OK | ok behaviour | tests/x |\n", encoding="utf-8")

    docs = discover_vb_documents(
        project_root, config=_config_docs("docs/test/vb.md")
    )
    resolved = {p.resolve() for p in docs}
    assert real.resolve() in resolved, (
        "in-root VB doc must still be discovered (anti-false-red)"
    )


# ---------------------------------------------------------------------------
# coverage_execution_coherence._test_case_keys_by_vb — reuses _iter_test_files
# ---------------------------------------------------------------------------
#
# This module reads covering test files via the SAME _iter_test_files /
# _resolve_vb_scan_dirs it imports from operational_e2e_audit. Driving the
# coherence covering-file read with a Path.read_text spy proves the out-of-root
# test is never read as covering evidence at THIS layer too.


def test_coherence_covering_files_outside_root_never_read(tmp_path, monkeypatch):
    # coverage_execution_coherence._authentic_cover_case_keys reads covering test
    # files via _iter_test_files(project_root, test_dirs=_resolve_vb_scan_dirs(...))
    # (the line-456 read). Replicate that exact call with scan.test_dirs pointed at
    # an absolute out-of-root dir and a read_text spy: the external test must never
    # be read as covering evidence.
    from codd.coverage_execution_coherence import (
        _iter_test_files as coherence_iter_test_files,
        _resolve_vb_scan_dirs as coherence_resolve_scan_dirs,
    )

    project_root = tmp_path / "project"
    project_root.mkdir()
    leak = _seed_outside_test(tmp_path)
    config = {"scan": {"test_dirs": [str(leak.parent)]}}

    read_paths: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self, *args, **kwargs):
        read_paths.append(Path(self).resolve())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy)
    for path in coherence_iter_test_files(
        project_root,
        test_dirs=coherence_resolve_scan_dirs(project_root, config),
    ):
        # mirror line 459: the covering-file scan reads each yielded file
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

    assert leak.resolve() not in read_paths, (
        "coherence covering-file scan read an out-of-root test file as evidence"
    )


# ---------------------------------------------------------------------------
# coverage_auditor._discover_existing_artifacts — artifact_discovery.paths /
# artifact_paths overrides (artifact-existence PASS credit)
# ---------------------------------------------------------------------------


def _write_codd_yaml(project_root: Path, body: str) -> None:
    codd_dir = project_root / ".codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    (codd_dir / "codd.yaml").write_text(body, encoding="utf-8")


def _seed_outside_artifact(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    art = outside / "requirements.md"
    art.write_text("# Requirements\n", encoding="utf-8")
    return art


@pytest.mark.parametrize("kind", ["parent", "absolute"])
def test_discover_existing_artifacts_outside_root_not_credited(tmp_path, kind):
    project_root = tmp_path / "project"
    project_root.mkdir()
    art = _seed_outside_artifact(tmp_path)
    raw = "../outside" if kind == "parent" else str(art.parent)
    _write_codd_yaml(project_root, f"artifact_discovery:\n  paths:\n    - {raw}\n")

    existing = CoverageAuditor(project_root)._discover_existing_artifacts(project_root)
    assert "design:requirements" not in existing, (
        f"{kind} out-of-root artifact_discovery.path credited an external artifact"
    )


def test_discover_existing_artifacts_symlink_escape_not_credited(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    art = _seed_outside_artifact(tmp_path)
    (project_root / "linked_design").symlink_to(art.parent)
    _write_codd_yaml(
        project_root, "artifact_discovery:\n  paths:\n    - linked_design\n"
    )

    existing = CoverageAuditor(project_root)._discover_existing_artifacts(project_root)
    assert "design:requirements" not in existing, (
        "in-root symlink escaping the root credited an external artifact"
    )


def test_discover_existing_artifacts_override_outside_root_not_credited(tmp_path):
    """The artifact_paths override branch (:565) must also jail out-of-root paths."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    art = _seed_outside_artifact(tmp_path)
    _write_codd_yaml(
        project_root,
        "artifact_discovery:\n"
        "  artifact_paths:\n"
        f"    design:requirements:\n      - {art}\n",
    )

    existing = CoverageAuditor(project_root)._discover_existing_artifacts(project_root)
    assert "design:requirements" not in existing, (
        "out-of-root artifact_paths override credited an external artifact"
    )


def test_discover_existing_artifacts_in_root_still_credited(tmp_path):
    """Anti-false-red: an in-root design doc is still discovered/credited."""
    project_root = tmp_path / "project"
    specs = project_root / "docs" / "specs"
    specs.mkdir(parents=True)
    (specs / "requirements.md").write_text("# Requirements\n", encoding="utf-8")
    _write_codd_yaml(
        project_root, "artifact_discovery:\n  paths:\n    - docs/specs\n"
    )

    existing = CoverageAuditor(project_root)._discover_existing_artifacts(project_root)
    assert "design:requirements" in existing, (
        "in-root design doc must still be credited (anti-false-red)"
    )
