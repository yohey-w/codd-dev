"""H1 — a formatter lives in the same repository as CoDD's canon (F-1).

Field incident: `npx prettier --write .` re-aligned the pipes of a 440-line
requirements table. A requirement unit IS a Markdown table row, so nothing about
the meaning changed: review saw a whitespace diff, every CoDD check stayed green,
and byte-identity with the human-approved original was gone. It was noticed only
because an unrelated diff check happened to run that day.

This file runs the real formatter against a real `codd init` project. The
negative control (guard removed, same formatter, same document) is what stops
the green from being vacuous: without it the test would also pass in a world
where prettier never touches Markdown at all.
"""

from __future__ import annotations

REQ = "docs/requirements/requirements.md"


def test_prettierignore_is_scaffolded_and_covers_the_canon(make_project):
    project = make_project()
    assert project.exists(".prettierignore"), (
        "`codd init` did not write .prettierignore — the F-1 guard is gone.\n"
        + project.init_output
    )
    ignored = project.read(".prettierignore")
    assert "docs/requirements/" in ignored, "canon dir not excluded from formatting"
    assert "docs/source/" in ignored, (
        "reference-material dir not excluded — CoDD tells users to park foreign "
        "documents in docs/source/, so the formatter guard must reach it too"
    )


def _drop_guard(project) -> None:
    """Delete .prettierignore, with a message that names the cause if absent."""
    guard = project.root / ".prettierignore"
    assert guard.exists(), (
        "`codd init` produced no .prettierignore, so there is no guard to remove "
        "— the F-1 protection is missing entirely (see the scaffolding test)."
    )
    guard.unlink()


def test_real_prettier_cannot_touch_the_canon(make_project, prettier):
    """The protection, and its negative control, on the same document."""
    project = make_project()
    before = project.sha(REQ)

    prettier(project)
    assert project.sha(REQ) == before, (
        "prettier rewrote the requirements document despite .prettierignore"
    )

    # ── negative control ───────────────────────────────────────────────
    # Remove the guard and run the identical command. If the bytes still do
    # not move, the assertion above proved nothing: this fixture simply is not
    # something prettier reformats, and the test would stay green after the
    # guard was deleted from `codd init`.
    _drop_guard(project)
    prettier(project)
    assert project.sha(REQ) != before, (
        "NEGATIVE CONTROL FAILED: with .prettierignore deleted, prettier still "
        "left the requirements document byte-identical. The protection test "
        "above is therefore vacuous — the fixture needs a table prettier "
        "actually reformats (ragged pipes)."
    )


def test_canon_ledger_catches_a_formatter_that_bypasses_prettierignore(
    make_project, prettier, canon_mechanism
):
    """The tool-independent layer: any writer, not just the one named tool.

    `.prettierignore` stops prettier. It does nothing about dprint, an editor's
    format-on-save, `markdownlint --fix`, or an agent editing the wrong file —
    and nothing at all for a project that already exists. When the canon ledger
    is present it must be green before and red after, whatever did the writing.
    """
    project = make_project()

    if not canon_mechanism:
        # No ledger in this build: the scaffolded ignore file is the only layer,
        # and it is already pinned above. Assert the limitation explicitly so
        # this branch is not a silent no-op — a project whose .prettierignore is
        # deleted (or which adopts CoDD after the fact) is unprotected, which is
        # the whole reason the ledger exists.
        assert not project.exists("codd/canon.lock"), (
            "canon.lock exists but conftest.canon_mechanism reported the "
            "mechanism absent — the behavioural probe is wrong"
        )
        _drop_guard(project)
        before = project.sha(REQ)
        prettier(project)
        assert project.sha(REQ) != before, "negative control: prettier did nothing"
        return

    # anti-false-red control: intact project must be green FIRST
    green = project.codd("canon", "status")
    assert green.exit_code == 0, (
        "a freshly initialised project is already red on canon integrity:\n"
        + green.output
    )

    _drop_guard(project)   # simulate a formatter CoDD cannot name
    prettier(project)

    red = project.codd("canon", "status")
    assert red.exit_code != 0, (
        "canon status stayed green after a formatter rewrote the canon:\n" + red.output
    )
    # Keyed on the document path, not on the severity word: the status prose is
    # another team's in-flight copy and rewording it must not turn this red.
    assert REQ in red.output or "requirements.md" in red.output, red.output
