"""H5 — `codd init` lands on a project that already exists (F-4).

`codd init` is almost never the first command in a repository. It runs after
`create-next-app`, `poetry new`, `cargo init` — each of which has already
written a `.gitignore` tuned to that stack, and possibly a `.prettierignore`
too. Clobbering those is a worse accident than the gap CoDD is closing: the
user loses ignore rules silently and starts committing build output, or worse,
loses an ignore rule that was keeping a secret out.

Both directions are asserted here, on both guard files:
  * present → byte-identical afterwards, and the user is told it was left alone;
  * absent  → written, with the content that makes it worth having.
The second half is the negative control for the first: "unchanged" is trivially
true for a file CoDD never writes under any circumstances.
"""

from __future__ import annotations

import pytest

EXISTING_GITIGNORE = """# written by the project's own scaffolder — do not clobber
/.next/
/coverage-custom/
.env.local
"""
EXISTING_PRETTIERIGNORE = """# the team's own formatter policy
vendor/
CHANGELOG.md
"""


@pytest.mark.parametrize(
    "filename, seeded",
    [(".gitignore", EXISTING_GITIGNORE), (".prettierignore", EXISTING_PRETTIERIGNORE)],
)
def test_init_never_overwrites_a_guard_file_that_already_exists(
    make_project, filename, seeded
):
    project = make_project(seed={filename: seeded})
    assert project.read(filename) == seeded, (
        f"`codd init` overwrote an existing {filename}. The scaffolder's rules "
        "are gone and nothing said so.\n" + project.init_output
    )
    assert f"{filename} already exists" in project.init_output, (
        f"{filename} was preserved but the user was never told CoDD skipped it "
        "— they will assume CoDD's protections are in place when they are "
        "not:\n" + project.init_output
    )


@pytest.mark.parametrize("filename", [".gitignore", ".prettierignore"])
def test_negative_control_a_fresh_project_does_get_the_guard_file(
    make_project, filename
):
    """Without this, 'unchanged' would also pass if CoDD wrote nothing, ever."""
    project = make_project()
    assert project.exists(filename), (
        f"NEGATIVE CONTROL FAILED: a project with no {filename} did not get one, "
        "so the preservation test above proves nothing."
    )


def test_the_scaffolded_gitignore_covers_the_first_real_accident(make_project):
    """F-4's actual motivation: a fresh project leaks secrets on commit 1."""
    project = make_project()
    assert project.exists(".gitignore"), (
        "`codd init` wrote no .gitignore at all — F-4 is open.\n" + project.init_output
    )
    ignored = project.read(".gitignore")
    assert ".env" in ignored, ignored
    assert "!.env.example" in ignored, (
        "`.env*` is ignored with no escape for the example file — the template "
        "CoDD tells users to commit would be silently untracked:\n" + ignored
    )
    # Deliberately narrow: a broad `*secret*` glob would silently untrack
    # legitimate sources (secret_manager.py) — the same "you cannot notice it"
    # failure class this whole suite exists for.
    assert "*secret*" not in ignored, (
        "a broad *secret* glob crept into the template; it untracks legitimate "
        "source files silently:\n" + ignored
    )
