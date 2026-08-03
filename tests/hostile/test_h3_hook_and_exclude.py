"""H3 — pre-commit hook and `scan.exclude` in the same repository (F-8).

CoDD's own advice for F-2 is "park reference material in docs/source/". The
advice was self-defeating: `docs/source/` sits under `doc_dirs: ["docs/"]`, and
both the pre-commit hook and the validator walked `doc_dirs` while ignoring
`scan.exclude` — so CoDD refused to commit a file it had itself declared out of
scope. Reference material is foreign text; it will never carry CoDD frontmatter.

The bug hid for a whole engagement because that project had no pre-commit hook
installed: the gate was never armed, so `docs/source/` looked fine. This suite
arms it. Every assertion here goes through a REAL `git commit` — the hook shells
out to the `codd` console script, so an in-process invocation would test a
different code path than the one that failed.

The reason string is checked, not just the exit code: with the canon ledger now
running before the frontmatter loop, "commit rejected" has more than one
possible cause and an exit-code-only assertion would pass for the wrong reason.
"""

from __future__ import annotations

FOREIGN = "docs/source/upstream-spec.md"
FOREIGN_TEXT = "# Upstream specification (received from the customer)\n\nNo CoDD frontmatter — this is foreign text.\n"


def test_reference_material_under_an_excluded_dir_can_be_committed(make_project):
    project = make_project(git=True)
    project.install_hook()
    project.write(FOREIGN, FOREIGN_TEXT)

    result = project.commit("add reference material")
    assert result.returncode == 0, (
        "the pre-commit hook rejected a file that codd.yaml itself excludes "
        "(F-8 regression):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_scaffolded_config_excludes_the_reference_dir_it_recommends(make_project):
    """The fix has two halves; this is the template half."""
    project = make_project()
    exclude = ((project.config().get("scan") or {}).get("exclude") or [])
    assert any("docs/source" in str(pattern) for pattern in exclude), (
        "codd.yaml recommends docs/source/ for reference material but does not "
        f"exclude it — the hook will reject it. exclude={exclude}"
    )


def test_negative_control_without_the_exclude_the_commit_is_rejected(make_project):
    """Reproduce the pre-fix world: same repo, exclude entry removed.

    This is what makes the green above load-bearing. If the hook rejected on
    some unrelated ground, or accepted everything unconditionally, this control
    would not produce the specific frontmatter error.
    """
    project = make_project(git=True)
    project.install_hook()

    text = project.config_text()
    stripped = "\n".join(
        line for line in text.splitlines() if "docs/source/**" not in line
    ) + "\n"
    assert stripped != text, "no docs/source/** entry to remove — fixture drifted"
    project.write("codd/codd.yaml", stripped)

    project.write(FOREIGN, FOREIGN_TEXT)
    result = project.commit("add reference material without the exclude")

    assert result.returncode != 0, (
        "NEGATIVE CONTROL FAILED: with docs/source/** removed from scan.exclude "
        "the commit still succeeded, so the acceptance test above does not "
        "actually depend on the exclude being honoured.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "missing CoDD YAML frontmatter" in combined, (
        "the commit was rejected for a different reason than the one under "
        f"test:\n{combined}"
    )
