"""Path-escape jail coverage for the four coverage/audit-layer readers.

Each reader here consumes a *user-controllable* path (a ``codd.yaml`` config
value or a ``--scenarios`` CLI argument) and reaches a filesystem read /
``is_file`` / ``rglob`` sink that either reads the file as **test evidence** or
credits its existence as a **PASS witness**. An absolute path, a ``../`` parent
traversal, or an in-root symlink whose target escapes the project root must NOT
be read or credited — otherwise an out-of-root file becomes a path-escape
false-green.

Fail-closed vs skip — the distinction this file pins (round-11):

* A **declared evidence ROOT / DOC** (the ``scan.test_dirs`` /
  ``test_coverage.docs`` / ``--scenarios`` entry the operator declared) that
  escapes the project root makes the audit **NOT VALID**: it raises
  :class:`codd.path_safety.PathEscapeError` (fail-closed) rather than being
  silently dropped. A silent skip there is a false-green in another form — the
  gate "passes" because it never saw the smuggled tree / had zero VB tables.
* A **per-file symlink encountered while walking a legitimate in-root tree** is
  still skipped (not raised): the declared root is correct, only one smuggled
  file inside it is dropped, and the rest of the in-root evidence is honoured.

Sinks pinned here:

* ``operational_e2e_audit._iter_test_files`` — ``scan.test_dirs`` (test evidence)
* ``verifiable_behavior_audit.discover_vb_documents`` — ``test_coverage.docs``
* ``operational_e2e_audit._load_or_extract_operational_scenarios`` —
  ``--scenarios`` operational scenario catalog (operational E2E audit evidence)
* ``coverage_execution_coherence._test_case_keys_by_vb`` — reuses
  ``_iter_test_files`` (``scan.test_dirs``) to read covering test files
* ``coverage_auditor._discover_existing_artifacts`` — ``artifact_discovery.paths``
  and ``artifact_paths`` overrides (artifact-existence PASS credit)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.coverage_auditor import CoverageAuditor
from codd.operational_e2e_audit import (
    _iter_test_files,
    _load_or_extract_operational_scenarios,
)
from codd.path_safety import PathEscapeError
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
def test_iter_test_files_declared_root_outside_fails_closed(tmp_path, kind):
    """A declared ``scan.test_dirs`` ROOT that escapes is fail-closed, not skipped.

    Silent-skip here means the evidence scan finds zero markers and the gate
    "passes" with no test evidence — a false-green. The declared evidence root
    escaping the project must raise instead.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    leak = _seed_outside_test(tmp_path)
    raw = "../outside" if kind == "parent" else str(leak.parent)

    with pytest.raises(PathEscapeError):
        list(_iter_test_files(project_root, test_dirs=[raw]))


def test_iter_test_files_declared_root_symlink_escape_fails_closed(tmp_path):
    """A declared ROOT that is itself a symlink escaping the tree is fail-closed."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    leak = _seed_outside_test(tmp_path)
    link = project_root / "linked_tests"
    link.symlink_to(leak.parent)

    with pytest.raises(PathEscapeError):
        list(_iter_test_files(project_root, test_dirs=["linked_tests"]))


def test_iter_test_files_per_file_symlink_escape_skipped_not_raised(tmp_path):
    """Per-file symlink escape INSIDE a valid in-root root is skipped, not raised.

    The declared root (``tests``) is in-root and correct; only one smuggled
    symlink file inside it points outside. That single file is dropped, the
    in-root sibling is still yielded, and NO PathEscapeError is raised
    (declared-root fail-closed vs per-file skip distinction).
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    real = _seed_in_root_test(project_root)  # tests/real.test.ts (in-root)
    leak = _seed_outside_test(tmp_path)
    # A symlinked test FILE inside the legitimate in-root tests/ dir.
    (project_root / "tests" / "smuggled.test.ts").symlink_to(leak)

    yielded = list(_iter_test_files(project_root, test_dirs=["tests"]))
    resolved = {p.resolve() for p in yielded}
    assert leak.resolve() not in resolved, (
        "per-file symlink escaping the root must be skipped (false-green guard)"
    )
    assert real.resolve() in resolved, (
        "in-root sibling test file must still be yielded (anti-false-red)"
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
def test_discover_vb_documents_declared_doc_outside_fails_closed(tmp_path, kind):
    """A declared ``test_coverage.docs`` DOC that escapes is fail-closed.

    Silent-skip here drops the only declared VB-table source → ``vb_count=0`` →
    the gate announces "no VB table found" and PASSES (false-green). The escape
    must raise instead.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _seed_outside_vb_doc(tmp_path)
    raw = "../outside/secret_vb.md" if kind == "parent" else str(doc)

    with pytest.raises(PathEscapeError):
        discover_vb_documents(project_root, config=_config_docs(raw))


def test_discover_vb_documents_declared_doc_symlink_escape_fails_closed(tmp_path):
    """A declared DOC that is itself a symlink escaping the tree is fail-closed."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _seed_outside_vb_doc(tmp_path)
    (project_root / "alias.md").symlink_to(doc)

    with pytest.raises(PathEscapeError):
        discover_vb_documents(project_root, config=_config_docs("alias.md"))


def test_discover_vb_documents_per_file_symlink_escape_skipped_not_raised(tmp_path):
    """Per-file symlink escape inside a declared in-root DIR is skipped, not raised.

    The declared ``test_coverage.docs`` entry is an in-root directory; the walk
    of its ``*.md`` finds a real in-root doc plus a smuggled symlink pointing
    outside. The symlink is dropped, the real doc is returned, no raise.
    """
    project_root = tmp_path / "project"
    docs_dir = project_root / "docs" / "test"
    docs_dir.mkdir(parents=True)
    real = docs_dir / "vb.md"
    real.write_text("| VB-OK | ok behaviour | tests/x |\n", encoding="utf-8")
    leak = _seed_outside_vb_doc(tmp_path)
    (docs_dir / "smuggled.md").symlink_to(leak)

    docs = discover_vb_documents(project_root, config=_config_docs("docs/test"))
    resolved = {p.resolve() for p in docs}
    assert leak.resolve() not in resolved, (
        "per-file symlink escaping the root must be skipped (false-green guard)"
    )
    assert real.resolve() in resolved, (
        "in-root sibling VB doc must still be returned (anti-false-red)"
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
# operational_e2e_audit._load_or_extract_operational_scenarios — --scenarios
# ---------------------------------------------------------------------------
#
# The operational scenario catalog supplied via ``codd e2e audit --scenarios``
# (and ``e2e workflow-plan --scenarios``) is operational E2E audit evidence: it
# defines the declared scenario universe the audit reconciles tests against. A
# declared ``--scenarios`` path that escapes the project root must be fail-closed
# (audit not valid), not silently routed to ``load_scenarios_from_markdown``
# where an out-of-root file would be parsed as the declared universe.


def _seed_outside_scenarios(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    cat = outside / "scenarios.md"
    cat.write_text("# Operational scenarios\n", encoding="utf-8")
    return cat


@pytest.mark.parametrize("kind", ["parent", "absolute"])
def test_load_scenarios_declared_path_outside_fails_closed(tmp_path, kind):
    project_root = tmp_path / "project"
    project_root.mkdir()
    cat = _seed_outside_scenarios(tmp_path)
    raw = "../outside/scenarios.md" if kind == "parent" else str(cat)

    with pytest.raises(PathEscapeError):
        _load_or_extract_operational_scenarios(project_root, scenarios_path=raw)


def test_load_scenarios_declared_symlink_escape_fails_closed(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    cat = _seed_outside_scenarios(tmp_path)
    (project_root / "scenarios.md").symlink_to(cat)

    with pytest.raises(PathEscapeError):
        _load_or_extract_operational_scenarios(
            project_root, scenarios_path="scenarios.md"
        )


def test_load_scenarios_in_root_path_still_loaded(tmp_path):
    """Anti-false-red: an in-root --scenarios catalog is still loaded."""
    project_root = tmp_path / "project"
    e2e_dir = project_root / "docs" / "e2e"
    e2e_dir.mkdir(parents=True)
    cat = e2e_dir / "custom-scenarios.md"
    cat.write_text("# Operational scenarios\n", encoding="utf-8")

    # Must not raise; returns a (possibly empty) collection, not an error.
    collection = _load_or_extract_operational_scenarios(
        project_root, scenarios_path="docs/e2e/custom-scenarios.md"
    )
    assert collection is not None


def test_load_scenarios_default_path_unaffected(tmp_path):
    """Anti-false-red: with no --scenarios, the in-root default path is used."""
    project_root = tmp_path / "project"
    e2e_dir = project_root / "docs" / "e2e"
    e2e_dir.mkdir(parents=True)
    (e2e_dir / "operational-scenarios.md").write_text(
        "# Operational scenarios\n", encoding="utf-8"
    )

    collection = _load_or_extract_operational_scenarios(
        project_root, scenarios_path=None
    )
    assert collection is not None


# ---------------------------------------------------------------------------
# coverage_execution_coherence._test_case_keys_by_vb — reuses _iter_test_files
# ---------------------------------------------------------------------------
#
# This module reads covering test files via the SAME _iter_test_files /
# _resolve_vb_scan_dirs it imports from operational_e2e_audit. A declared
# scan.test_dirs root pointing out-of-root must now fail-closed at THIS layer
# too (the shared _iter_test_files raise propagates), never silently reading an
# external test as covering evidence.


def test_coherence_covering_files_declared_root_outside_fails_closed(tmp_path):
    from codd.coverage_execution_coherence import (
        _iter_test_files as coherence_iter_test_files,
        _resolve_vb_scan_dirs as coherence_resolve_scan_dirs,
    )

    project_root = tmp_path / "project"
    project_root.mkdir()
    leak = _seed_outside_test(tmp_path)
    config = {"scan": {"test_dirs": [str(leak.parent)]}}

    with pytest.raises(PathEscapeError):
        list(
            coherence_iter_test_files(
                project_root,
                test_dirs=coherence_resolve_scan_dirs(project_root, config),
            )
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
