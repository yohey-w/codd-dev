"""Regression tests for validation retries in codd fix PHENOMENON mode."""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from click.testing import CliRunner

from codd.cli import main
from codd.fix import phenomenon_fixer
from codd.fix.design_updater import DesignUpdateError, apply_update, update_design_doc
from codd.fix.phenomenon_fixer import run_phenomenon_fix
from codd.fix.phenomenon_parser import PhenomenonAnalysis
from tests.fix.test_phenomenon_fixer import _write_project


PHENOMENON = "login error wording is unclear"
TARGET = "design/login.md"
DESIGN_HEADER = "title: Login\ndescription: login form\n"
REQUIRED_ENTRIES = "acceptance_criteria:\n  - id: c1\n    description: clear error\n"
CODD_BLOCK = "codd:\n  node_id: login\n  # Keep this tooling comment.\n  band: 'green'\n"
BODY = "# Login\n\nThe login form shows an error.\n"
NEW_BODY = BODY + "Use a specific message.\n"
ORIGINAL = f"---\n{DESIGN_HEADER}{REQUIRED_ENTRIES}{CODD_BLOCK}---\n{BODY}"
SHRINK_ERROR = (
    "design_updater: 'acceptance_criteria' entries reduced "
    "(1 → 0); use --allow-delete to override"
)
EMPTY_ERROR = "LLM returned empty document body"
TAMPER_ERROR = "design_updater: frontmatter 'codd:' block was modified (tooling-owned)"


def _scripted_design_ai(*design_responses):
    responses = iter(design_responses)
    design_prompts = []

    def invoke(prompt: str) -> str:
        if "\nCURRENT_DOCUMENT_PATH: " in prompt:
            design_prompts.append(prompt)
            return next(responses)
        if prompt.startswith("You are CoDD's PHENOMENON parser."):
            return json.dumps({
                "intent": "improvement",
                "subject_terms": ["login"],
                "lexicon_hits": ["login"],
                "ambiguity_score": 0.05,
            })
        if prompt.startswith("You are CoDD's semantic matcher."):
            return "{}"
        if prompt.startswith("You are CoDD's risk classifier."):
            return '{"risky": false, "categories": []}'
        raise AssertionError(f"Unexpected AI prompt: {prompt}")

    return invoke, design_prompts


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _assert_retry_prompt(prompt: str, attempt: int, reason: str) -> None:
    assert reason in prompt
    assert re.search(rf"\battempt\s*#?\s*{attempt}\b", prompt, re.IGNORECASE)


def test_first_design_prompt_matches_pre_change_bytes(tmp_path):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    ai, prompts = _scripted_design_ai(ORIGINAL)

    result = run_phenomenon_fix(
        project, PHENOMENON, ai_invoke=ai, non_interactive=True, propagate_impl=False
    )

    assert not result.aborted, result.abort_reason
    (prompt,) = prompts
    path_line = f"\nCURRENT_DOCUMENT_PATH: {project / TARGET}\n"
    assert prompt.count(path_line) == 1
    normalized = prompt.replace(path_line, f"\nCURRENT_DOCUMENT_PATH: {TARGET}\n")
    assert _prompt_hash(normalized) == (
        "d5a02082a1bede75b85979974e12fb6b538c9bbdef8b713629a27e6eb51b7490"
    )


@pytest.mark.parametrize("omit_frontmatter", [False, True])
def test_omitted_codd_block_is_restored_verbatim(tmp_path, omit_frontmatter):
    if omit_frontmatter:
        original = ORIGINAL.replace(REQUIRED_ENTRIES, "")
        proposed = NEW_BODY
    else:
        original = ORIGINAL
        proposed = original.replace(CODD_BLOCK, "").replace(BODY, NEW_BODY)
    project = _write_project(tmp_path, {TARGET: original})
    ai, prompts = _scripted_design_ai(proposed, proposed, proposed)

    result = run_phenomenon_fix(
        project, PHENOMENON, ai_invoke=ai, non_interactive=True, propagate_impl=False
    )

    assert result.applied_paths == [TARGET], result.attempts
    assert len(prompts) == 1
    updated = (project / TARGET).read_text(encoding="utf-8")
    opening, frontmatter, body = updated.split("---\n", 2)
    assert not opening
    assert frontmatter.count(CODD_BLOCK) == 1
    assert body == NEW_BODY
    if not omit_frontmatter:
        assert frontmatter.replace(CODD_BLOCK, "") == DESIGN_HEADER + REQUIRED_ENTRIES


def test_dry_run_retry_only_includes_the_latest_validation_error(tmp_path):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    shrunk = ORIGINAL.replace(REQUIRED_ENTRIES, "")
    updated = ORIGINAL.replace(BODY, NEW_BODY)
    ai, prompts = _scripted_design_ai(shrunk, "", updated)

    result = run_phenomenon_fix(
        project, PHENOMENON, ai_invoke=ai, non_interactive=True,
        propagate_impl=False, dry_run=True,
    )

    assert [attempt.attempt for attempt in result.attempts] == [1, 2, 3]
    first_failure, second_failure, preview = result.attempts
    assert first_failure.update is None and SHRINK_ERROR in first_failure.aborted_reason
    assert second_failure.update is None and EMPTY_ERROR in second_failure.aborted_reason
    assert preview.update is not None and preview.update.proposed_content == updated
    assert not result.applied_paths
    assert (project / TARGET).read_text(encoding="utf-8") == ORIGINAL
    first, second, third = prompts
    assert SHRINK_ERROR not in first and EMPTY_ERROR not in first
    _assert_retry_prompt(second, 2, SHRINK_ERROR)
    assert EMPTY_ERROR not in second
    _assert_retry_prompt(third, 3, EMPTY_ERROR)
    assert SHRINK_ERROR not in third


def test_cli_rejects_changed_codd_block_and_reports_each_retry(tmp_path, monkeypatch):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    tampered = ORIGINAL.replace("'green'", "'amber'").replace(BODY, NEW_BODY)
    ai, prompts = _scripted_design_ai(tampered, tampered, tampered)
    monkeypatch.setattr(
        phenomenon_fixer, "_build_default_ai_invoke", lambda *_args, **_kwargs: ai
    )

    result = CliRunner().invoke(main, [
        "fix", PHENOMENON, "--path", str(project), "--non-interactive",
        "--no-propagate-impl", "--no-push",
    ])

    assert result.exit_code == 1, result.output
    assert result.output.count(f"reason: design_updater: {TAMPER_ERROR}") == 3
    for attempt in (1, 2, 3):
        assert f"Attempt {attempt} on {TARGET}" in result.output
    assert (project / TARGET).read_text(encoding="utf-8") == ORIGINAL
    first, second, third = prompts
    assert TAMPER_ERROR not in first
    assert len({_prompt_hash(prompt) for prompt in prompts}) == 3
    _assert_retry_prompt(second, 2, TAMPER_ERROR)
    _assert_retry_prompt(third, 3, TAMPER_ERROR)


def test_cli_interactive_allow_delete_still_applies_after_a_retry(tmp_path, monkeypatch):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    shrunk = ORIGINAL.replace(REQUIRED_ENTRIES, "").replace(BODY, NEW_BODY)
    ai, prompts = _scripted_design_ai("", shrunk)
    monkeypatch.setattr(
        phenomenon_fixer, "_build_default_ai_invoke", lambda *_args, **_kwargs: ai
    )

    result = CliRunner().invoke(main, [
        "fix", PHENOMENON, "--path", str(project), "--allow-delete",
        "--no-propagate-impl", "--no-push",
    ], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Apply update to design/login.md?" in result.output
    assert (project / TARGET).read_text(encoding="utf-8") == shrunk
    first, second = prompts
    assert "ALLOW_DELETE: true" in first and "ALLOW_DELETE: true" in second
    _assert_retry_prompt(second, 2, EMPTY_ERROR)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_omitted_codd_block_keeps_yaml_comments_and_line_endings(tmp_path, newline):
    target = tmp_path / "design.md"
    block = "codd :\n# Comment inside the block.\n  node_id: example  \n"
    original = f"---\n{block}title: Example\n---\n# Example\n"
    expected = original.replace("# Example\n", "# Example\nUpdated body.\n")
    target.write_bytes(original.replace("\n", newline).encode("utf-8"))

    update = update_design_doc(
        target,
        phenomenon_text="clarify the body",
        analysis=PhenomenonAnalysis(),
        ai_invoke=lambda _prompt: expected.replace(block, ""),
    )
    apply_update(update)

    assert target.read_bytes() == expected.replace("\n", newline).encode("utf-8")


def test_changed_codd_literal_trailing_spaces_are_rejected(tmp_path):
    target = tmp_path / "design.md"
    original = "---\ncodd:\n  summary: |-\n    text with trailing spaces  \n---\n# Example\n"
    target.write_text(original, encoding="utf-8")
    changed_value = original.replace("spaces  \n", "spaces\n")

    with pytest.raises(DesignUpdateError, match="tooling-owned"):
        update_design_doc(
            target,
            phenomenon_text="clarify the body",
            analysis=PhenomenonAnalysis(),
            ai_invoke=lambda _prompt: changed_value,
        )


@pytest.mark.parametrize(
    ("original", "proposed", "expected"),
    [
        (
            "---\ncodd:\n  node_id: example\n---",
            "# Example\n",
            "---\ncodd:\n  node_id: example\n---\n# Example\n",
        ),
        (
            "---\ncodd: &metadata\n  node_id: example\ncopy: *metadata\n---\n# Example\n",
            "---\ncopy: *metadata\n---\n# Updated example\n",
            "---\ncodd: &metadata\n  node_id: example\ncopy: *metadata\n---\n# Updated example\n",
        ),
    ],
    ids=["no-final-newline", "referenced-by-alias"],
)
def test_omitted_codd_block_restores_surrounding_yaml_structure(
    tmp_path, original, proposed, expected
):
    target = tmp_path / "design.md"
    target.write_text(original, encoding="utf-8")

    update = update_design_doc(
        target,
        phenomenon_text="clarify the body",
        analysis=PhenomenonAnalysis(),
        ai_invoke=lambda _prompt: proposed,
    )

    assert update.proposed_content == expected


def test_required_section_is_still_protected_with_cr_line_endings(tmp_path):
    target = tmp_path / "design.md"
    target.write_bytes(f"---\n{REQUIRED_ENTRIES}---\n# Example\n".replace("\n", "\r").encode())

    with pytest.raises(DesignUpdateError, match="acceptance_criteria.*entries reduced"):
        update_design_doc(
            target,
            phenomenon_text="clarify the body",
            analysis=PhenomenonAnalysis(),
            ai_invoke=lambda _prompt: "# Updated example\n",
        )
