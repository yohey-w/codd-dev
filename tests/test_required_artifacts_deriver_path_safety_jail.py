"""Path-escape jail coverage for RequiredArtifactsDeriver's requirement-doc reads.

``codd``'s required-artifact derivation reads requirement documents from two
*user-controllable* path sources and feeds their contents to the AI prompt as
evidence:

* explicit ``requirement_docs`` arguments (CLI / caller supplied);
* ``codd.yaml`` ``required_artifacts.requirement_docs`` (configured paths).

Both flow through ``RequiredArtifactsDeriver._read_requirement_docs`` →
``Path.read_text``. These tests pin the three escape fixtures the shared
:func:`codd.path_safety.resolve_project_path` jail must reject —

  1. ``../outside`` parent traversal,
  2. an absolute path outside the project root,
  3. an in-root symlink whose target escapes the root

— proving the out-of-root file is neither read nor folded into the requirement
text (evidence), plus an in-root regression (anti-false-red). The escape is
*excluded* (skipped), never crashed-on and never silently treated as a clean
empty read. ``runner.py`` is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.required_artifacts_deriver import RequiredArtifactsDeriver

SECRET = "TOP-SECRET-REQUIREMENT-MARKER"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path, *, config: str = "{}\n") -> Path:
    project = tmp_path / "project"
    codd_dir = project / ".codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(config, encoding="utf-8")
    return project


def _seed_outside_doc(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    secret = outside / "secret_requirements.md"
    secret.write_text(f"# secret\n{SECRET}\n", encoding="utf-8")
    return secret


# ---------------------------------------------------------------------------
# explicit requirement_docs argument
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("escape", ["parent", "absolute"])
def test_explicit_doc_out_of_root_not_read(tmp_path, escape):
    project = _make_project(tmp_path)
    secret = _seed_outside_doc(tmp_path)

    raw = "../outside/secret_requirements.md" if escape == "parent" else str(secret)
    deriver = RequiredArtifactsDeriver(project)

    # Out-of-root is excluded; with no in-root candidate, derivation reports
    # "no requirement documents" (not a clean empty read of the secret).
    with pytest.raises(FileNotFoundError):
        deriver._read_requirement_docs([raw])


def test_explicit_doc_in_root_symlink_escape_not_read(tmp_path):
    project = _make_project(tmp_path)
    secret = _seed_outside_doc(tmp_path)
    (project / "leak.md").symlink_to(secret)

    deriver = RequiredArtifactsDeriver(project)

    with pytest.raises(FileNotFoundError):
        deriver._read_requirement_docs(["leak.md"])


def test_explicit_doc_in_root_still_read(tmp_path):
    """Anti-false-red: an in-root requirement doc is still read as evidence."""
    project = _make_project(tmp_path)
    (project / "docs").mkdir()
    (project / "docs" / "requirements.md").write_text(
        "# real\nIN-ROOT-REQUIREMENT\n", encoding="utf-8"
    )

    deriver = RequiredArtifactsDeriver(project)
    text = deriver._read_requirement_docs(["docs/requirements.md"])

    assert "IN-ROOT-REQUIREMENT" in text


def test_explicit_doc_in_root_alongside_out_of_root_reads_only_in_root(tmp_path):
    """A mix of in-root and out-of-root paths reads the in-root one and drops
    the escape — the secret never enters the requirement text."""
    project = _make_project(tmp_path)
    secret = _seed_outside_doc(tmp_path)
    (project / "docs").mkdir()
    (project / "docs" / "requirements.md").write_text(
        "# real\nIN-ROOT-REQUIREMENT\n", encoding="utf-8"
    )

    deriver = RequiredArtifactsDeriver(project)
    text = deriver._read_requirement_docs(
        ["docs/requirements.md", str(secret)]
    )

    assert "IN-ROOT-REQUIREMENT" in text
    assert SECRET not in text, "out-of-root doc leaked into requirement evidence"


# ---------------------------------------------------------------------------
# codd.yaml required_artifacts.requirement_docs (configured discovery)
# ---------------------------------------------------------------------------


def test_configured_doc_out_of_root_not_read(tmp_path):
    secret = _seed_outside_doc(tmp_path)
    project = _make_project(
        tmp_path,
        config=(
            "required_artifacts:\n"
            f"  requirement_docs: ['{secret.as_posix()}']\n"
        ),
    )

    deriver = RequiredArtifactsDeriver(project)
    # No explicit docs -> falls back to configured discovery; the configured
    # out-of-root path is excluded, leaving nothing to read.
    discovered = deriver._discover_requirement_docs()

    assert all(SECRET not in p.read_text(encoding="utf-8") for p in discovered if p.exists())
    assert all(p.resolve() != secret.resolve() for p in discovered), (
        "configured out-of-root requirement_docs path survived discovery"
    )
