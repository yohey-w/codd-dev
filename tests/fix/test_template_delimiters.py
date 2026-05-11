"""Regression tests for cmd_471 Issue #24 — `---` delimiter collision.

Background: ``design_update.txt`` and ``risk_assessment.txt`` wrapped their
substituted content blocks (``{current_content}`` / ``{diff_text}``) between
two ``---`` markers. Markdown frontmatter also uses ``---`` as its fence, so
when the wrapped content was a design_doc the rendered prompt contained four
or more consecutive ``---`` lines. LLM responses occasionally echoed the
extra ``---`` separators back into the document body and corrupted the
frontmatter parser downstream.

The fix replaces ``---`` wrappers with unambiguous XML-style tags
(``<document>``/``<diff>``). These tests pin that the templates never re-grow
``---`` wrappers around substitution slots.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codd.fix.templates_loader import load_template, render_template


def _design_doc_with_frontmatter() -> str:
    """A realistic markdown design_doc — frontmatter uses `---` as fence."""
    return (
        "---\n"
        "title: Login\n"
        "codd:\n"
        "  node_id: auth_login\n"
        "---\n"
        "# Login\n"
        "Body content.\n"
    )


def _unified_diff_fixture() -> str:
    """Unified diffs naturally include `--- a/path` lines — a separate trap."""
    return (
        "--- a/src/app/page.tsx\n"
        "+++ b/src/app/page.tsx\n"
        "@@ -1,3 +1,3 @@\n"
        "-old line\n"
        "+new line\n"
    )


def test_design_update_template_does_not_wrap_content_with_triple_dash():
    text = load_template("design_update.txt")
    # The fixed template uses <document>...</document>; the legacy bug shape
    # was an `---` line immediately before and after {current_content}.
    assert re.search(r"^---\s*$\n\{current_content\}", text, re.MULTILINE) is None, (
        "design_update.txt still wraps {current_content} with `---` — that "
        "collides with markdown frontmatter (cmd_471 Issue #24)."
    )
    assert "<document>" in text and "</document>" in text


def test_design_update_rendered_prompt_has_no_ambiguous_dash_runs():
    """When rendered with a frontmatter doc, the prompt should keep the
    wrapping tags intact and never produce `---` lines that belong to the
    *template wrapper* rather than the document itself."""
    template = load_template("design_update.txt")
    rendered = render_template(
        template,
        phenomenon_text="login error wording",
        analysis_json="{}",
        allow_delete="false",
        target_path="docs/design/auth_login.md",
        current_content=_design_doc_with_frontmatter(),
    )
    # The document's own frontmatter survives inside the wrapper.
    assert "<document>" in rendered
    assert "</document>" in rendered
    # The wrapper itself is NOT made of `---` lines.
    pre, _, after = rendered.partition("<document>\n")
    inside, _, post = after.partition("\n</document>")
    # The substring immediately before <document> must not be a bare `---`.
    assert not pre.rstrip().endswith("\n---"), (
        "lines just above <document> include a bare `---` separator — "
        "would re-introduce the cmd_471 Issue #24 collision."
    )
    # The substring immediately after </document> must not be a bare `---`.
    assert not post.lstrip().startswith("---\n"), (
        "lines just below </document> include a bare `---` separator."
    )
    # The frontmatter's own `---` pair is preserved inside the document.
    assert inside.count("---") >= 2  # frontmatter open + close


def test_risk_assessment_template_does_not_wrap_diff_with_triple_dash():
    text = load_template("risk_assessment.txt")
    assert re.search(r"^---\s*$\n\{diff_text\}", text, re.MULTILINE) is None, (
        "risk_assessment.txt still wraps {diff_text} with `---` — collides "
        "with unified-diff `--- a/path` lines (cmd_471 Issue #24)."
    )
    assert "<diff>" in text and "</diff>" in text


def test_risk_assessment_rendered_prompt_keeps_diff_markers_unambiguous():
    template = load_template("risk_assessment.txt")
    rendered = render_template(
        template,
        diff_text=_unified_diff_fixture(),
        heuristic_flags="[]",
    )
    assert "<diff>" in rendered and "</diff>" in rendered
    # `--- a/path` lines from the diff itself survive inside the tags.
    assert "--- a/src/app/page.tsx" in rendered
    # The wrapper tags themselves are NOT `---` lines.
    pre_tag, _, after_tag = rendered.partition("<diff>\n")
    assert not pre_tag.rstrip().endswith("\n---")
