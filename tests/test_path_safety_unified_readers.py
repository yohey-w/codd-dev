"""Path-escape jail coverage for readers unified onto :mod:`codd.path_safety`.

Each reader here consumes a *user-controllable* path (codd.yaml config value or a
CLI arg) and reaches a filesystem read/exists/glob sink. These tests pin, per
reader, the three escape fixtures the shared jail must reject —

  1. ``../outside`` parent traversal
  2. an absolute path outside the project root
  3. an in-root symlink whose target escapes the root

— proving the external file is neither read nor used as a PASS witness, plus an
in-root regression (anti-false-red: a legitimate in-root path still works).

Covers the newly-jailed/hardened sinks (the three named readers and the
config_fs closure sites already have dedicated suites in
``test_config_fs_path_root_jail.py`` and ``dag/test_implementation_coverage_path_matcher.py``):

* ``propagator._find_design_docs_by_modules`` — ``wave_config[*].output``
* ``screen_transition_extractor.extract_transitions`` — ``src_dirs`` arg / config
* ``e2e_extractor._configured_doc_files`` — ``scan.doc_dirs`` (per-file symlink gap)
* ``cli._plan_design_doc_nodes`` — ``--design-doc`` args (implement plan / plan derive)
* ``cli.extract_design`` — ``extract design --design-doc`` (hash/extract evidence)
* ``cli.llm_derive`` — ``llm derive DESIGN_DOC`` (read_text -> considerations evidence)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.e2e_extractor import _configured_doc_files
from codd.propagator import _find_design_docs_by_modules
from codd.screen_transition_extractor import extract_transitions


# ---------------------------------------------------------------------------
# propagator._find_design_docs_by_modules — wave_config[*].output
# ---------------------------------------------------------------------------


def _wave_config(output: str) -> dict:
    return {
        "wave_config": {
            "wave1": [
                {"node_id": "leaked", "modules": ["m"], "output": output, "title": "leak"}
            ]
        }
    }


def _outside_doc(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = outside / "secret_design.md"
    doc.write_text("---\ncodd:\n  modules: [m]\n---\n# secret\n", encoding="utf-8")
    return doc


@pytest.mark.parametrize("make_output", ["parent", "absolute"])
def test_wave_config_output_outside_root_not_affected(tmp_path, make_output):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_doc(tmp_path)
    raw = "../outside/secret_design.md" if make_output == "parent" else str(doc)

    affected = _find_design_docs_by_modules(
        project_root, _wave_config(raw), {"m"}, {}
    )
    assert all(a.node_id != "leaked" for a in affected), (
        "wave_config output outside project root was treated as affected"
    )
    assert all("secret_design" not in a.path for a in affected)


def test_wave_config_output_in_root_symlink_escape_not_affected(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_doc(tmp_path)
    link = project_root / "alias.md"
    link.symlink_to(doc)

    affected = _find_design_docs_by_modules(
        project_root, _wave_config("alias.md"), {"m"}, {}
    )
    assert all(a.node_id != "leaked" for a in affected), (
        "in-root symlink escaping the root was treated as affected"
    )


def test_wave_config_output_in_root_still_affected(tmp_path):
    """Anti-false-red: an in-root wave_config output is still affected."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "doc.md").write_text("# d\n", encoding="utf-8")

    affected = _find_design_docs_by_modules(
        project_root, _wave_config("doc.md"), {"m"}, {}
    )
    assert any(a.node_id == "leaked" and a.path == "doc.md" for a in affected), (
        "in-root wave_config output must still be reported as affected"
    )


# ---------------------------------------------------------------------------
# screen_transition_extractor.extract_transitions — src_dirs arg / config
# ---------------------------------------------------------------------------

_TRANSITION_SRC = "import { useRouter } from 'next/router';\n"


def _seed_outside_source(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.tsx").write_text(_TRANSITION_SRC, encoding="utf-8")
    return outside


def test_screen_transition_src_dirs_parent_traversal_not_walked(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _seed_outside_source(tmp_path)

    read_paths: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self, *args, **kwargs):
        read_paths.append(Path(self).resolve())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy)
    extract_transitions(project_root, src_dirs=["../outside"])
    assert all("leak.tsx" not in str(p) for p in read_paths), (
        "src_dirs parent-traversal walked/read an out-of-root source file"
    )


def test_screen_transition_src_dirs_absolute_outside_not_walked(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = _seed_outside_source(tmp_path)

    read_paths: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self, *args, **kwargs):
        read_paths.append(Path(self).resolve())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy)
    extract_transitions(project_root, src_dirs=[str(outside)])
    assert all("leak.tsx" not in str(p) for p in read_paths), (
        "absolute out-of-root src_dir walked/read an external source file"
    )


def test_screen_transition_src_dirs_symlink_escape_not_walked(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = _seed_outside_source(tmp_path)
    link = project_root / "linked_src"
    link.symlink_to(outside)

    read_paths: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self, *args, **kwargs):
        read_paths.append(Path(self).resolve())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy)
    extract_transitions(project_root, src_dirs=["linked_src"])
    assert all("leak.tsx" not in str(p) for p in read_paths), (
        "in-root symlink to an outside dir walked/read an external source file"
    )


def test_screen_transition_src_dirs_per_file_symlink_escape_not_read(tmp_path, monkeypatch):
    # A real in-root src dir containing a symlink FILE whose target escapes the root:
    # the root is jailed, but each walked file must also be re-confined.
    project_root = tmp_path / "project"
    src = project_root / "src"
    src.mkdir(parents=True)
    (src / "real.tsx").write_text(_TRANSITION_SRC, encoding="utf-8")
    outside = _seed_outside_source(tmp_path)
    (src / "evil.tsx").symlink_to(outside / "leak.tsx")

    read_paths: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self, *args, **kwargs):
        read_paths.append(Path(self).resolve())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy)
    extract_transitions(project_root, src_dirs=["src"])
    assert all("leak.tsx" not in str(p) for p in read_paths), (
        "per-file symlink inside an in-root src dir escaped the root and was read"
    )
    assert any("real.tsx" in str(p) for p in read_paths), (
        "in-root source file must still be read (anti-false-red)"
    )


# ---------------------------------------------------------------------------
# e2e_extractor._configured_doc_files — scan.doc_dirs (per-file symlink gap)
# ---------------------------------------------------------------------------


def test_doc_dirs_per_file_symlink_escape_dropped(tmp_path):
    """An in-root doc-dir containing a symlink to an outside file must not
    enumerate that file (per-file symlink jail inside the rglob)."""
    project_root = tmp_path / "project"
    docs = project_root / "docs"
    docs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.md"
    secret.write_text("# secret\n", encoding="utf-8")
    (docs / "leak.md").symlink_to(secret)
    # A real in-root doc so the directory itself resolves/enumerates.
    (docs / "real.md").write_text("# real\n", encoding="utf-8")

    files = _configured_doc_files(project_root, {"scan": {"doc_dirs": ["docs/"]}})
    names = {p.name for p in files}
    resolved = {p.resolve() for p in files}
    assert secret.resolve() not in resolved, (
        "doc_dir symlink escaping the root was enumerated"
    )
    assert "real.md" in names, "in-root doc must still be enumerated (anti-false-red)"


def test_doc_dirs_absolute_outside_not_enumerated(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# secret\n", encoding="utf-8")

    files = _configured_doc_files(project_root, {"scan": {"doc_dirs": [str(outside)]}})
    assert all("secret" not in p.name for p in files), (
        "absolute out-of-root doc_dir was enumerated"
    )


def test_read_optional_context_file_rejects_out_of_root(tmp_path):
    # codd.yaml lexicon_path / design_md_path -> _read_optional_context_file must jail
    # via path_safety, so an absolute or symlink-escaping config value is not read.
    from codd.cli import _read_optional_context_file

    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.yaml"
    secret.write_text("secret: leaked\n", encoding="utf-8")

    # absolute out-of-root config path is not read
    assert _read_optional_context_file(project_root, str(secret)) is None
    # an in-root symlink whose target escapes the root is not read
    (project_root / "link.yaml").symlink_to(secret)
    assert _read_optional_context_file(project_root, "link.yaml") is None
    # a legitimate in-root file is still read (anti-false-red)
    (project_root / "ctx.md").write_text("# context\n", encoding="utf-8")
    assert _read_optional_context_file(project_root, "ctx.md") == "# context\n"


def test_configured_doc_files_rejects_out_of_root(tmp_path):
    # codd.yaml scan.doc_dirs feeds doc evidence -> must jail via path_safety, so an
    # absolute or symlink-escaping doc_dir cannot read out-of-root files as evidence.
    from codd.cli import _configured_doc_files

    project_root = tmp_path / "project"
    (project_root / "docs").mkdir(parents=True)
    (project_root / "docs" / "real.md").write_text("# real\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# secret\n", encoding="utf-8")

    # absolute out-of-root doc_dir is not enumerated
    files = _configured_doc_files(project_root, {"scan": {"doc_dirs": [str(outside)]}})
    assert all("secret" not in p.name for p in files)
    # in-root doc_dir still enumerated (anti-false-red)
    files = _configured_doc_files(project_root, {"scan": {"doc_dirs": ["docs/"]}})
    assert any(p.name == "real.md" for p in files)
    # an in-root symlink whose target escapes the root is not enumerated
    (project_root / "docs" / "leak.md").symlink_to(outside / "secret.md")
    files = _configured_doc_files(project_root, {"scan": {"doc_dirs": ["docs/"]}})
    assert all((outside / "secret.md").resolve() != p.resolve() for p in files)


# ---------------------------------------------------------------------------
# cli._plan_design_doc_nodes — CLI --design-doc args (implement plan / plan derive)
# ---------------------------------------------------------------------------
#
# These design-doc args feed a DAG ``design_doc`` node (id/path/frontmatter) that is
# read as the source of derived implementation steps / tasks. An out-of-root doc
# (``../``, absolute, or in-root symlink escaping the tree) must NOT be turned into a
# node — otherwise an external file's frontmatter/body is consumed as design evidence
# (a path-escape false-green).


def _outside_design_doc(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = outside / "secret_design.md"
    doc.write_text(
        "---\ncodd:\n  node_id: leaked\n---\n# secret design\n", encoding="utf-8"
    )
    return doc


def test_plan_design_doc_nodes_parent_traversal_not_noded(tmp_path):
    from codd.cli import _plan_design_doc_nodes

    project_root = tmp_path / "project"
    project_root.mkdir()
    _outside_design_doc(tmp_path)

    with pytest.raises((FileNotFoundError, ValueError)):
        _plan_design_doc_nodes(project_root, ("../outside/secret_design.md",))


def test_plan_design_doc_nodes_absolute_outside_not_noded(tmp_path):
    from codd.cli import _plan_design_doc_nodes

    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_design_doc(tmp_path)

    with pytest.raises((FileNotFoundError, ValueError)):
        _plan_design_doc_nodes(project_root, (str(doc),))


def test_plan_design_doc_nodes_symlink_escape_not_noded(tmp_path):
    from codd.cli import _plan_design_doc_nodes

    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_design_doc(tmp_path)
    (project_root / "alias.md").symlink_to(doc)

    with pytest.raises((FileNotFoundError, ValueError)):
        _plan_design_doc_nodes(project_root, ("alias.md",))


def test_plan_design_doc_nodes_in_root_still_noded(tmp_path):
    """Anti-false-red: an in-root design doc still produces a node."""
    from codd.cli import _plan_design_doc_nodes

    project_root = tmp_path / "project"
    (project_root / "docs").mkdir(parents=True)
    (project_root / "docs" / "real.md").write_text(
        "---\ncodd:\n  node_id: real\n---\n# real design\n", encoding="utf-8"
    )

    nodes = _plan_design_doc_nodes(project_root, ("docs/real.md",))
    assert [n.path for n in nodes] == ["docs/real.md"]
    assert all("secret" not in (n.path or "") for n in nodes)


# ---------------------------------------------------------------------------
# cli.extract_design — `extract design --design-doc` (hash/extract as evidence)
# ---------------------------------------------------------------------------
#
# extract design hashes the doc and extracts expected-artifact evidence from it.
# An out-of-root doc must be rejected before is_file/read_text/extract/cache.


def _invoke_extract_design(project_root: Path, design_doc: str):
    from click.testing import CliRunner

    from codd.cli import extract_design

    return CliRunner().invoke(
        extract_design,
        ["--project-path", str(project_root), "--design-doc", design_doc],
    )


def _seed_codd_project(project_root: Path) -> None:
    (project_root / ".codd").mkdir(parents=True, exist_ok=True)


def _assert_extract_design_rejected_before_extraction(result, project_root: Path):
    # Rejection must happen at the jail (``design document not found``), BEFORE any
    # extraction runs — never accidentally non-zero because the AI command is missing.
    assert result.exit_code != 0
    assert "not found" in result.output
    assert "AI command" not in result.output
    assert not list((project_root / ".codd").rglob("*secret_design*"))


def test_extract_design_parent_traversal_rejected(tmp_path):
    project_root = tmp_path / "project"
    _seed_codd_project(project_root)
    _outside_design_doc(tmp_path)

    result = _invoke_extract_design(project_root, "../outside/secret_design.md")
    _assert_extract_design_rejected_before_extraction(result, project_root)


def test_extract_design_absolute_outside_rejected(tmp_path):
    project_root = tmp_path / "project"
    _seed_codd_project(project_root)
    doc = _outside_design_doc(tmp_path)

    result = _invoke_extract_design(project_root, str(doc))
    _assert_extract_design_rejected_before_extraction(result, project_root)


def test_extract_design_symlink_escape_rejected(tmp_path):
    project_root = tmp_path / "project"
    _seed_codd_project(project_root)
    doc = _outside_design_doc(tmp_path)
    (project_root / "alias.md").symlink_to(doc)

    result = _invoke_extract_design(project_root, "alias.md")
    _assert_extract_design_rejected_before_extraction(result, project_root)


# ---------------------------------------------------------------------------
# cli.llm_derive — `llm derive DESIGN_DOC` (read_text -> considerations evidence)
# ---------------------------------------------------------------------------


def _invoke_llm_derive_spying_reads(project_root: Path, design_doc: str, monkeypatch):
    """Invoke ``llm derive`` while recording every ``Path.read_text`` target.

    Proves the *invariant* (the out-of-root secret is never read) regardless of
    which layer rejects it — click's ``exists=True`` blocks ``../``/symlink paths
    (resolved from cwd), while the shared jail blocks an absolute out-of-root path
    that click would otherwise accept.
    """
    from click.testing import CliRunner

    from codd.cli import llm_derive

    read_paths: list[Path] = []
    real_read_text = Path.read_text

    def _spy(self, *args, **kwargs):
        read_paths.append(Path(self).resolve())
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _spy)
    result = CliRunner().invoke(
        llm_derive, [design_doc, "--project-path", str(project_root)]
    )
    return result, read_paths


def _assert_secret_never_read(result, read_paths, secret: Path):
    assert result.exit_code != 0
    assert all(
        secret.resolve() != p for p in read_paths
    ), "out-of-root design doc was read as considerations source"


def test_llm_derive_parent_traversal_rejected(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    _seed_codd_project(project_root)
    secret = _outside_design_doc(tmp_path)

    result, reads = _invoke_llm_derive_spying_reads(
        project_root, "../outside/secret_design.md", monkeypatch
    )
    _assert_secret_never_read(result, reads, secret)


def test_llm_derive_absolute_outside_rejected(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    _seed_codd_project(project_root)
    secret = _outside_design_doc(tmp_path)

    result, reads = _invoke_llm_derive_spying_reads(
        project_root, str(secret), monkeypatch
    )
    _assert_secret_never_read(result, reads, secret)


def test_llm_derive_symlink_escape_rejected(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    _seed_codd_project(project_root)
    secret = _outside_design_doc(tmp_path)
    (project_root / "alias.md").symlink_to(secret)

    result, reads = _invoke_llm_derive_spying_reads(
        project_root, "alias.md", monkeypatch
    )
    _assert_secret_never_read(result, reads, secret)


# ---------------------------------------------------------------------------
# anti-false-red: in-root CLI design docs must still pass the jail (reach extraction)
# ---------------------------------------------------------------------------
#
# No AI command is configured in the test env, so a jailed-but-accepted in-root doc
# is expected to get PAST the jail and fail later at the AI step. The discriminator
# is the message: "design document not found" == false-rejected by the jail (a
# regression); "AI command" == the jail accepted it and extraction/derivation began.


def _seed_in_root_design_doc(project_root: Path) -> str:
    (project_root / "docs").mkdir(parents=True, exist_ok=True)
    (project_root / "docs" / "real.md").write_text(
        "---\ncodd:\n  node_id: real\n---\n# real design\n", encoding="utf-8"
    )
    return "docs/real.md"


def test_extract_design_in_root_passes_jail(tmp_path):
    project_root = tmp_path / "project"
    _seed_codd_project(project_root)
    rel = _seed_in_root_design_doc(project_root)

    result = _invoke_extract_design(project_root, rel)
    assert "design document not found" not in result.output, (
        "in-root design doc was false-rejected by the jail (anti-false-red)"
    )


def test_llm_derive_in_root_passes_jail(tmp_path):
    from click.testing import CliRunner

    from codd.cli import llm_derive

    project_root = tmp_path / "project"
    _seed_codd_project(project_root)
    rel = _seed_in_root_design_doc(project_root)

    result = CliRunner().invoke(
        llm_derive, [rel, "--project-path", str(project_root)]
    )
    assert "design document not found" not in result.output, (
        "in-root design doc was false-rejected by the jail (anti-false-red)"
    )
