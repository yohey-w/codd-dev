"""Path-escape jail coverage for ``discover_requirement_docs``.

``discover_requirement_docs`` resolves user-controllable
``requirement_reconciliation.docs`` (codd.yaml) — and the default
``docs/requirements/**`` discovery — into the requirement-doc list whose
contents become reconciliation evidence (and, downstream,
``operations_derive`` derivation evidence).

A configured path that is absolute, ``../`` traversal, or an in-root symlink
whose target escapes the project root must NOT be surfaced — otherwise an
out-of-root document is read as requirement evidence. The posture is
fail-closed: an out-of-root configured path yields *no* requirement doc from
that entry (it cannot manufacture a false reconciliation anchor). These tests
pin the three escape fixtures the shared
:func:`codd.path_safety.resolve_project_path` jail must reject, plus in-root
regressions (anti-false-red). ``runner.py`` is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.requirement_reconciliation import discover_requirement_docs


def _seed_outside_doc(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    doc = outside / "secret_req.md"
    doc.write_text("# secret\n", encoding="utf-8")
    return doc


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _resolved(paths) -> set[Path]:
    return {p.resolve() for p in paths}


# --- configured docs escape ---------------------------------------------------


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_configured_doc_out_of_root_excluded(tmp_path, escape):
    root = _make_root(tmp_path)
    secret = _seed_outside_doc(tmp_path)
    raw = "../outside/secret_req.md" if escape == "parent" else str(secret)

    docs = discover_requirement_docs(
        root, {"requirement_reconciliation": {"docs": [raw]}}
    )

    assert secret.resolve() not in _resolved(docs), (
        "out-of-root configured requirement doc was surfaced as evidence"
    )
    assert docs == []


def test_configured_doc_in_root_symlink_escape_excluded(tmp_path):
    root = _make_root(tmp_path)
    secret = _seed_outside_doc(tmp_path)
    (root / "leak.md").symlink_to(secret)

    docs = discover_requirement_docs(
        root, {"requirement_reconciliation": {"docs": ["leak.md"]}}
    )

    assert secret.resolve() not in _resolved(docs), (
        "in-root symlink escaping the root was surfaced as a requirement doc"
    )


def test_configured_dir_with_symlinked_md_escape_excluded(tmp_path):
    """A configured *directory* whose rglob turns up an in-root *.md symlinked
    outside the root must re-confine that match (rglob follows symlinks)."""
    root = _make_root(tmp_path)
    secret = _seed_outside_doc(tmp_path)
    reqdir = root / "reqs"
    reqdir.mkdir()
    (reqdir / "real.md").write_text("# real in-root\n", encoding="utf-8")
    (reqdir / "leak.md").symlink_to(secret)

    docs = discover_requirement_docs(
        root, {"requirement_reconciliation": {"docs": ["reqs"]}}
    )

    resolved = _resolved(docs)
    assert (reqdir / "real.md").resolve() in resolved, (
        "in-root requirement doc dropped (false-red)"
    )
    assert secret.resolve() not in resolved, (
        "symlinked-out *.md inside a configured dir leaked as evidence"
    )


# --- default discovery escape -------------------------------------------------


def test_default_discovery_symlink_escape_excluded(tmp_path):
    root = _make_root(tmp_path)
    secret = _seed_outside_doc(tmp_path)
    req_dir = root / "docs" / "requirements"
    req_dir.mkdir(parents=True)
    (req_dir / "real.md").write_text("# real\n", encoding="utf-8")
    (req_dir / "leak.md").symlink_to(secret)

    docs = discover_requirement_docs(root, {})

    resolved = _resolved(docs)
    assert (req_dir / "real.md").resolve() in resolved
    assert secret.resolve() not in resolved, (
        "symlinked-out *.md under docs/requirements/ leaked as evidence"
    )


# --- anti-false-red: in-root docs still discovered ----------------------------


def test_configured_in_root_doc_still_discovered(tmp_path):
    root = _make_root(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "other.md").write_text("# real\n", encoding="utf-8")

    docs = discover_requirement_docs(
        root, {"requirement_reconciliation": {"docs": ["docs/other.md"]}}
    )

    assert (root / "docs" / "other.md").resolve() in _resolved(docs)


def test_default_in_root_doc_still_discovered(tmp_path):
    root = _make_root(tmp_path)
    (root / "requirements.md").write_text("# real\n", encoding="utf-8")

    docs = discover_requirement_docs(root, {})

    assert (root / "requirements.md").resolve() in _resolved(docs)
