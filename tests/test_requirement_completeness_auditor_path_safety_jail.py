"""Path-escape jail coverage for RequirementCompletenessAuditor doc reads.

The requirement-completeness audit reads requirement documents from a
*user-controllable* explicit ``requirement_docs`` list and feeds their contents
to the ASK-generation prompt as evidence, via
``RequirementCompletenessAuditor._read_requirement_docs`` → ``Path.read_text``.

These tests pin the three escape fixtures the shared
:func:`codd.path_safety.resolve_project_path` jail must reject —

  1. ``../outside`` parent traversal,
  2. an absolute path outside the project root,
  3. an in-root symlink whose target escapes the root

— proving the out-of-root file is neither read nor folded into the requirement
text, plus an in-root regression (anti-false-red). The escape is *excluded*
(skipped) rather than crashed-on. ``runner.py`` is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.requirement_completeness_auditor import RequirementCompletenessAuditor

SECRET = "TOP-SECRET-REQUIREMENT-MARKER"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    codd_dir = project / ".codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text("{}\n", encoding="utf-8")
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
    auditor = RequirementCompletenessAuditor(project)
    text = auditor._read_requirement_docs([raw])

    assert SECRET not in text, (
        "out-of-root requirement doc was read into the audit evidence text"
    )


def test_explicit_doc_in_root_symlink_escape_not_read(tmp_path):
    project = _make_project(tmp_path)
    secret = _seed_outside_doc(tmp_path)
    (project / "leak.md").symlink_to(secret)

    auditor = RequirementCompletenessAuditor(project)
    text = auditor._read_requirement_docs(["leak.md"])

    assert SECRET not in text, (
        "in-root symlink escaping the root was read into the audit evidence text"
    )


def test_explicit_doc_in_root_still_read(tmp_path):
    """Anti-false-red: an in-root requirement doc is still read as evidence."""
    project = _make_project(tmp_path)
    (project / "docs").mkdir()
    (project / "docs" / "requirements.md").write_text(
        "# real\nIN-ROOT-REQUIREMENT\n", encoding="utf-8"
    )

    auditor = RequirementCompletenessAuditor(project)
    text = auditor._read_requirement_docs(["docs/requirements.md"])

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

    auditor = RequirementCompletenessAuditor(project)
    text = auditor._read_requirement_docs(
        ["docs/requirements.md", str(secret)]
    )

    assert "IN-ROOT-REQUIREMENT" in text
    assert SECRET not in text, "out-of-root doc leaked into audit evidence"
