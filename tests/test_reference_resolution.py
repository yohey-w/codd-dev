"""Tests for the deterministic reference-resolution contract (ACG axis-1).

Covers ``codd.reference_resolution.resolve_document_ref`` +
``codd.scanner.build_document_reference_index`` and the
``build_document_node_path_map`` backward-compat wrapper.

The governing safety property under test: a SUT-supplied reference must bind to
EXACTLY ONE registered document, recovery is allowed ONLY for unambiguous
basename-only / doc-root+basename forms, and ambiguous / unresolved /
wrong-subcategory references honest-fail (raised as ``FileNotFoundError``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd.reference_resolution import (
    ReferenceResolutionError,
    basename_recovery_allowed,
    record_reference_resolution_event,
    resolve_document_ref,
)
from codd.scanner import (
    DocumentEntry,
    build_document_node_path_map,
    build_document_reference_index,
)


# ── fixtures ─────────────────────────────────────────────────────────


def _write_doc(project: Path, rel: str, node_id: str, *, body: str = "Body\n") -> Path:
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ncodd:\n  node_id: \"{node_id}\"\n  type: design\n---\n\n# {node_id}\n{body}",
        encoding="utf-8",
    )
    return path


def _config(doc_dirs: list[str] | None = None) -> dict:
    return {"scan": {"doc_dirs": doc_dirs or ["docs/"]}}


@pytest.fixture
def project_with_api_doc(tmp_path: Path) -> Path:
    """A project whose ONLY api doc lives at docs/design/api_interface_contract.md."""
    project = tmp_path / "project"
    project.mkdir()
    _write_doc(
        project,
        "docs/design/api_interface_contract.md",
        "design:api-interface-contract",
    )
    return project


# ── exact path / node_id / alias ─────────────────────────────────────


def test_exact_path_resolves(project_with_api_doc: Path):
    index = build_document_reference_index(project_with_api_doc, _config())
    binding = resolve_document_ref(
        "docs/design/api_interface_contract.md",
        project_root=project_with_api_doc,
        index=index,
        producer="t1",
        ref_kind="source_design_doc",
    )
    assert binding.method == "exact_path"
    assert binding.canonical_path == "docs/design/api_interface_contract.md"
    assert binding.canonical_id == "design:api-interface-contract"
    assert binding.recovered is False


def test_unregistered_exact_path_binds_as_doc_id(tmp_path: Path):
    """A file that exists exactly as referenced but carries no CoDD frontmatter
    binds to a synthetic ``doc:<path>`` identity (NOT a guess — the path is
    real). This preserves the pre-contract behavior where an existing exact path
    always resolved, so legacy / brownfield docs without frontmatter keep
    working. Honest-fail is reserved for refs that do not point at a real file."""
    project = tmp_path / "project"
    project.mkdir()
    plain = project / "docs" / "design" / "plain.md"
    plain.parent.mkdir(parents=True)
    plain.write_text("# no frontmatter\n", encoding="utf-8")

    index = build_document_reference_index(project, _config())
    binding = resolve_document_ref(
        "docs/design/plain.md",
        project_root=project,
        index=index,
        producer="t1",
        ref_kind="source_design_doc",
    )
    assert binding.method == "exact_path_unregistered"
    assert binding.canonical_path == "docs/design/plain.md"
    assert binding.canonical_id == "doc:docs/design/plain.md"
    assert binding.recovered is False


def test_exact_node_id_resolves(project_with_api_doc: Path):
    index = build_document_reference_index(project_with_api_doc, _config())
    binding = resolve_document_ref(
        "design:api-interface-contract",
        project_root=project_with_api_doc,
        index=index,
        producer="t1",
        ref_kind="source_design_doc",
    )
    assert binding.method == "exact_node_id"
    assert binding.canonical_path == "docs/design/api_interface_contract.md"


def test_node_id_collision_fails(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _write_doc(project, "docs/design/a.md", "design:dupe")
    _write_doc(project, "docs/spec/b.md", "design:dupe")
    config = _config(["docs/"])

    index = build_document_reference_index(project, config)
    assert any(c.kind == "node_id" and c.key == "design:dupe" for c in index.collisions)

    with pytest.raises(ReferenceResolutionError) as exc:
        resolve_document_ref(
            "design:dupe",
            project_root=project,
            index=index,
            producer="t1",
            ref_kind="source_design_doc",
        )
    assert exc.value.reason == "ambiguous_node_id"
    assert len(exc.value.candidates) == 2


def test_alias_resolves(project_with_api_doc: Path):
    index = build_document_reference_index(project_with_api_doc, _config())
    binding = resolve_document_ref(
        "doc:docs/design/api_interface_contract.md",
        project_root=project_with_api_doc,
        index=index,
        producer="t1",
        ref_kind="source_design_doc",
    )
    assert binding.method == "exact_alias"
    assert binding.canonical_path == "docs/design/api_interface_contract.md"


# ── BUG REPRO: unique basename recovery ──────────────────────────────


def test_bug_repro_basename_only_recovered(project_with_api_doc: Path):
    """`api_interface_contract.md` (basename only) → recovered to the real doc."""
    index = build_document_reference_index(project_with_api_doc, _config())
    binding = resolve_document_ref(
        "api_interface_contract.md",
        project_root=project_with_api_doc,
        index=index,
        producer="t1",
        ref_kind="source_design_doc",
    )
    assert binding.method == "unique_basename_recovered"
    assert binding.recovered is True
    assert binding.canonical_path == "docs/design/api_interface_contract.md"


def test_bug_repro_docroot_plus_basename_recovered(project_with_api_doc: Path):
    """`docs/api_interface_contract.md` (doc_root + basename) → recovered.

    This is the exact greenfield failure: the SUT wrote
    ``docs/api_interface_contract.md`` but the real doc is under ``docs/design/``.
    """
    index = build_document_reference_index(project_with_api_doc, _config())
    binding = resolve_document_ref(
        "docs/api_interface_contract.md",
        project_root=project_with_api_doc,
        index=index,
        producer="implement_e2e_routing_suite",
        ref_kind="source_design_doc",
    )
    assert binding.method == "unique_basename_recovered"
    assert binding.recovered is True
    assert binding.canonical_path == "docs/design/api_interface_contract.md"
    assert binding.canonical_id == "design:api-interface-contract"


# ── CRITICAL SAFETY TEST: wrong subcategory must NOT recover ─────────


def test_wrong_subcategory_honest_fail_not_recovered(project_with_api_doc: Path):
    """`docs/test/api_interface_contract.md` asserts subdir `test/` which is NOT
    the real `design/`. Even though the basename is unique, this MUST honest-fail
    (the SUT may be hallucinating a *different* document). This is the
    anti-false-green safety floor."""
    index = build_document_reference_index(project_with_api_doc, _config())
    with pytest.raises(ReferenceResolutionError) as exc:
        resolve_document_ref(
            "docs/test/api_interface_contract.md",
            project_root=project_with_api_doc,
            index=index,
            producer="t1",
            ref_kind="source_design_doc",
        )
    # Not recovered — unresolved (the guard blocked basename recovery).
    assert exc.value.reason == "unresolved_reference"


def test_basename_recovery_allowed_guard_unit(project_with_api_doc: Path):
    """Direct unit coverage of the guard: only basename-only / doc_root+basename
    forms are allowed; a concrete conflicting subpath is rejected."""
    index = build_document_reference_index(project_with_api_doc, _config())
    entry = index.by_basename["api_interface_contract.md"][0]
    roots = index.doc_roots

    assert basename_recovery_allowed("api_interface_contract.md", entry, roots) is True
    assert basename_recovery_allowed("docs/api_interface_contract.md", entry, roots) is True
    # Concrete (wrong) subcategory → NOT allowed.
    assert basename_recovery_allowed("docs/test/api_interface_contract.md", entry, roots) is False
    assert basename_recovery_allowed("docs/design/api_interface_contract.md", entry, roots) is False


# ── ambiguous basename ───────────────────────────────────────────────


def test_ambiguous_basename_honest_fail_with_candidates(tmp_path: Path):
    """Two docs share a basename; a basename-only ref is ambiguous → honest-fail
    listing both candidates (never a guess)."""
    project = tmp_path / "project"
    project.mkdir()
    _write_doc(project, "docs/design/contract.md", "design:contract-a")
    _write_doc(project, "docs/spec/contract.md", "design:contract-b")
    config = _config(["docs/"])

    index = build_document_reference_index(project, config)
    with pytest.raises(ReferenceResolutionError) as exc:
        resolve_document_ref(
            "contract.md",
            project_root=project,
            index=index,
            producer="t1",
            ref_kind="source_design_doc",
        )
    assert exc.value.reason == "ambiguous_basename"
    assert set(exc.value.candidates) == {
        "docs/design/contract.md",
        "docs/spec/contract.md",
    }


# ── zero match / node-id-like-but-unregistered ───────────────────────


def test_zero_match_honest_fail(project_with_api_doc: Path):
    index = build_document_reference_index(project_with_api_doc, _config())
    with pytest.raises(ReferenceResolutionError) as exc:
        resolve_document_ref(
            "totally_made_up.md",
            project_root=project_with_api_doc,
            index=index,
            producer="t1",
            ref_kind="source_design_doc",
        )
    assert exc.value.reason == "unresolved_reference"


def test_node_id_like_but_unregistered_fails_no_basename_fallback(project_with_api_doc: Path):
    """A node-id-shaped string that is not registered must NOT fall back to
    basename recovery (node-like refs only resolve via exact node_id)."""
    index = build_document_reference_index(project_with_api_doc, _config())
    with pytest.raises(ReferenceResolutionError) as exc:
        resolve_document_ref(
            "design:does-not-exist",
            project_root=project_with_api_doc,
            index=index,
            producer="t1",
            ref_kind="source_design_doc",
        )
    assert exc.value.reason == "unresolved_reference"


def test_allow_recovery_false_disables_basename_recovery(project_with_api_doc: Path):
    index = build_document_reference_index(project_with_api_doc, _config())
    with pytest.raises(ReferenceResolutionError):
        resolve_document_ref(
            "docs/api_interface_contract.md",
            project_root=project_with_api_doc,
            index=index,
            producer="t1",
            ref_kind="source_design_doc",
            allow_recovery=False,
        )


# ── security cutoffs ─────────────────────────────────────────────────


def test_empty_reference_fails(project_with_api_doc: Path):
    index = build_document_reference_index(project_with_api_doc, _config())
    with pytest.raises(ReferenceResolutionError) as exc:
        resolve_document_ref(
            "   ",
            project_root=project_with_api_doc,
            index=index,
            producer="t1",
            ref_kind="source_design_doc",
        )
    assert exc.value.reason == "empty_reference"


def test_escapes_project_fails(project_with_api_doc: Path):
    index = build_document_reference_index(project_with_api_doc, _config())
    with pytest.raises(ReferenceResolutionError) as exc:
        resolve_document_ref(
            "../secrets/api_interface_contract.md",
            project_root=project_with_api_doc,
            index=index,
            producer="t1",
            ref_kind="source_design_doc",
        )
    assert exc.value.reason == "reference_escapes_project"


def test_in_root_symlink_file_escaping_root_fails(tmp_path: Path):
    """An IN-ROOT relative ref whose file is a SYMLINK escaping the tree must fail.

    The old string-only ``_escapes_project`` jail (no ``..``, no leading ``/``)
    let an in-root symlink (``docs/leak.md`` → an off-root file) sail through and
    bind as a real, existing exact path — consuming an OUT-OF-ROOT file as
    evidence (a path-escape false-green). Unifying on the symlink-resolving
    ``path_safety`` closure must reject it.
    """
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("# off-root secret\n", encoding="utf-8")
    link = project / "docs" / "leak.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    index = build_document_reference_index(project, _config())
    with pytest.raises(ReferenceResolutionError) as exc:
        resolve_document_ref(
            "docs/leak.md",  # in-root path string, but the file escapes via symlink
            project_root=project,
            index=index,
            producer="t1",
            ref_kind="source_design_doc",
        )
    assert exc.value.reason == "reference_escapes_project"


def test_absolute_in_root_symlink_escaping_root_fails(tmp_path: Path):
    """An ABSOLUTE in-root ref that is a symlink escaping the tree must fail too.

    Guards the absolute-path branch's confinement on the SAME unified resolver
    (resolve + symlink-follow), not just the relative branch.
    """
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("# off-root secret\n", encoding="utf-8")
    link = project / "docs" / "leak.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    index = build_document_reference_index(project, _config())
    with pytest.raises(ReferenceResolutionError) as exc:
        resolve_document_ref(
            str(link),  # absolute path to the in-root symlink that escapes
            project_root=project,
            index=index,
            producer="t1",
            ref_kind="source_design_doc",
        )
    assert exc.value.reason == "reference_escapes_project"


# ── audit sink ───────────────────────────────────────────────────────


def test_record_reference_resolution_event_appends_jsonl(project_with_api_doc: Path):
    index = build_document_reference_index(project_with_api_doc, _config())
    binding = resolve_document_ref(
        "docs/api_interface_contract.md",
        project_root=project_with_api_doc,
        index=index,
        producer="t1",
        ref_kind="source_design_doc",
    )
    record_reference_resolution_event(
        project_with_api_doc, binding, stage="plan_derivation", status="recovered"
    )
    audit = project_with_api_doc / ".codd" / "audit" / "reference_resolution.jsonl"
    assert audit.exists()
    line = json.loads(audit.read_text(encoding="utf-8").strip())
    assert line["event"] == "reference_resolution"
    assert line["status"] == "recovered"
    assert line["method"] == "unique_basename_recovered"
    assert line["canonical_path"] == "docs/design/api_interface_contract.md"
    assert line["stage"] == "plan_derivation"


# ── backward compatibility: build_document_node_path_map unchanged ───


def test_build_document_node_path_map_backward_compat(project_with_api_doc: Path):
    """The legacy node_id->Path map must be byte-for-byte unchanged in shape."""
    mapping = build_document_node_path_map(project_with_api_doc, _config())
    assert mapping == {
        "design:api-interface-contract": Path("docs/design/api_interface_contract.md")
    }


def test_build_document_node_path_map_multi_doc(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    _write_doc(project, "docs/design/a.md", "design:a")
    _write_doc(project, "docs/requirements/b.md", "req:b")
    mapping = build_document_node_path_map(project, _config(["docs/"]))
    assert mapping == {
        "design:a": Path("docs/design/a.md"),
        "req:b": Path("docs/requirements/b.md"),
    }
