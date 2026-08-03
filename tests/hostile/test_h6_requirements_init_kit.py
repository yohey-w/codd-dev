"""H6 — the whole `codd init --requirements` kit arrives together (F-1..F-4).

The individual guards are pinned in H1/H2/H5. This file pins the *set*: a user
who runs the documented first command gets, in one shot, every layer the field
findings asked for. It is the test that fails when a future refactor keeps each
piece alive but stops one of them being scaffolded.

Template assertions read the RAW text of codd/codd.yaml, not `yaml.safe_load`:
the `requirement_reconciliation` block ships commented out (it is advisory, and
switching it on by default would put warnings in front of projects that never
declared operation_flow), so the parsed document does not contain it at all.
"""

from __future__ import annotations


def test_init_with_requirements_scaffolds_every_layer(make_project):
    project = make_project()

    missing = [
        rel for rel in (
            ".prettierignore",                    # F-1 formatter guard
            ".gitignore",                         # F-4 secret guard
            "codd/codd.yaml",
            "docs/requirements/requirements.md",
        ) if not project.exists(rel)
    ]
    assert not missing, f"`codd init --requirements` did not produce {missing}\n{project.init_output}"


def test_the_imported_requirements_document_gets_frontmatter(make_project):
    """Otherwise CoDD's own pre-commit hook rejects the file CoDD just wrote."""
    project = make_project()
    text = project.read("docs/requirements/requirements.md")
    assert text.startswith("---"), text[:200]
    assert "node_id:" in text.split("---")[1], text[:400]
    assert "type: requirement" in text.split("---")[1], text[:400]


def test_reconciliation_template_is_present_and_documented(make_project):
    """F-3: the mechanism must be discoverable without reading CoDD's source.

    The engagement that found this had to open
    `codd/requirement_reconciliation.py` to learn what `sections` and
    `out_of_scope_markers` meant, because the generated config said nothing.
    """
    text = project_config_text(make_project())
    assert "requirement_reconciliation:" in text, (
        "no requirement_reconciliation template in codd.yaml — a user has no "
        "way to learn the mechanism exists"
    )
    for key in ("sections:", "out_of_scope_markers:", "enabled:"):
        assert key in text, f"{key} missing from the reconciliation template"


def test_double_counting_warning_is_in_the_config_the_user_edits(make_project):
    """F-2's mitigation is documentary, so the document is the deliverable."""
    text = project_config_text(make_project())
    assert "docs/requirements/ is CANON" in text, text
    assert "docs/source/" in text, (
        "the config warns against the trap but never names the alternative "
        "location:\n" + text
    )


def test_init_output_names_every_file_it_wrote(make_project):
    """A guard the user does not know about is a guard they will delete."""
    out = make_project().init_output
    assert "codd.yaml" in out, out
    assert "requirements" in out, out


def project_config_text(project) -> str:
    return project.config_text()
