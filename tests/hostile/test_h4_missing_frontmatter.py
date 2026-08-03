"""H4 — the other side of F-8: a normal document with no frontmatter.

The F-8 fix taught two gates to honour `scan.exclude`. The risk in any fix of
that shape is that it opens too wide — if `scan.exclude` were consulted with a
looser matcher, or the walk were relaxed rather than filtered, CoDD would stop
rejecting the thing it is supposed to reject and the gate would become
decorative.

So this file asserts the *refusal* still happens for a document that is NOT
excluded, and asserts it by reason: the canon ledger now runs ahead of the
frontmatter loop in `run_pre_commit`, so an exit-code-only assertion could go
green on canon drift while the frontmatter gate was completely dead.
"""

from __future__ import annotations

IN_SCOPE = "docs/design/system_design.md"
NO_FRONTMATTER = "# System design\n\nA normal CoDD document that forgot its frontmatter.\n"
WITH_FRONTMATTER = """---
codd:
  node_id: "design:system-design"
  type: design
  status: draft
  confidence: 0.6
---

# System design

A normal CoDD document that has its frontmatter.
"""


def test_in_scope_document_without_frontmatter_is_still_rejected(make_project):
    project = make_project(git=True)
    project.install_hook()
    project.write(IN_SCOPE, NO_FRONTMATTER)

    result = project.commit("add a design doc with no frontmatter")

    assert result.returncode != 0, (
        "a document under doc_dirs with no CoDD frontmatter was committed — the "
        "F-8 exclude fix opened the gate too wide.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "missing CoDD YAML frontmatter" in combined, (
        "rejected, but not for the missing frontmatter — the frontmatter gate "
        f"may be dead and something else may be failing the commit:\n{combined}"
    )
    assert IN_SCOPE in combined, (
        f"the rejection does not name the offending file:\n{combined}"
    )


def test_the_same_document_with_frontmatter_commits(make_project):
    """Anti-false-red control: the gate must not simply reject everything."""
    project = make_project(git=True)
    project.install_hook()
    project.write(IN_SCOPE, WITH_FRONTMATTER)

    result = project.commit("add a well-formed design doc")
    assert result.returncode == 0, (
        "a correctly formed CoDD document was rejected — the test above would "
        "then be passing for a reason that has nothing to do with frontmatter.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_exclude_does_not_silence_a_document_outside_the_excluded_dir(make_project):
    """`scan.exclude: docs/source/**` must not be read as `docs/**`.

    A near-miss path (docs/sourced/, not docs/source/) is the cheap way to catch
    a matcher that was made too permissive while fixing F-8.
    """
    project = make_project(git=True)
    project.install_hook()
    project.write("docs/sourced/notes.md", NO_FRONTMATTER)

    result = project.commit("near-miss path")
    combined = result.stdout + result.stderr
    assert result.returncode != 0 and "missing CoDD YAML frontmatter" in combined, (
        "docs/sourced/ (a near miss for the excluded docs/source/) was waved "
        f"through — the exclude matcher is too loose:\n{combined}"
    )
