"""Regression coverage for PHENOMENON update validation and retry feedback."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

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
SHRINK_ERROR = (
    "design_updater: 'acceptance_criteria' entries reduced "
    "(1 → 0); use --allow-delete to override"
)
EMPTY_ERROR = "LLM returned empty document body"
TAMPER_ERROR = "design_updater: frontmatter 'codd:' block was modified (tooling-owned)"
MISSING_CODD_ERROR = "frontmatter 'codd:' block is missing (tooling-owned)"
LEADING_TEXT_ERROR = "must start with its original frontmatter"
MISSING_FRONTMATTER_ERROR = "frontmatter is missing or incomplete"
RECOVERY_WARNING = "discarded a short preface before the frontmatter."
PREFACE = "Here is the updated document.\n\n"
REJECTED_MARKER = "PRIVATE REJECTED PROPOSAL MARKER"


def _doc(header=DESIGN_HEADER + REQUIRED_ENTRIES + CODD_BLOCK, body=BODY):
    return f"---\n{header}---\n{body}"


ORIGINAL = _doc()
UPDATED = _doc(body=NEW_BODY)
NO_CODD = _doc(DESIGN_HEADER + REQUIRED_ENTRIES, NEW_BODY)
INLINE_CODD = _doc(DESIGN_HEADER + "codd: {node_id: login}\n", NEW_BODY)
QUOTED_CODD = _doc(DESIGN_HEADER + "'codd':\n  node_id: login\n", NEW_BODY)
SPACED_CODD = _doc(DESIGN_HEADER + "codd :\n  node_id: login\n", NEW_BODY)
NESTED_CODD = _doc(DESIGN_HEADER + "outer:\n  codd:\n    node_id: login\n", NEW_BODY)
DUPLICATE_CODD = _doc(DESIGN_HEADER + REQUIRED_ENTRIES + CODD_BLOCK + CODD_BLOCK, NEW_BODY)
LITERAL_CODD = _doc(DESIGN_HEADER + "codd:\n  summary: |-\n    text with spaces  \n")
ANCHOR_CODD = _doc(DESIGN_HEADER + "codd: &metadata\n  node_id: login\ncopy: *metadata\n")
MALFORMED_YAML = _doc(DESIGN_HEADER + "broken: [\n" + REQUIRED_ENTRIES + CODD_BLOCK, NEW_BODY)
DEEPLY_NESTED_YAML = _doc(
    "metadata: " + "[" * 800 + "x" + "]" * 800 + "\n" + DESIGN_HEADER + REQUIRED_ENTRIES + CODD_BLOCK,
    NEW_BODY,
)
SECOND_HEADER = _doc("title: Another document\n", "")
ADJACENT_HEADERS = _doc(body="") + SECOND_HEADER + NEW_BODY
CRLF_ORIGINAL = ORIGINAL.replace("\n", "\r\n")
BOM_ORIGINAL = "\ufeff" + ORIGINAL
BASELINE_REVISION = "947df64b4668f9bedd8075fc6386f7b1169c9d07"


@pytest.fixture(scope="module")
def legacy_design_updater():
    """Load the exact baseline when available, including in local worktrees."""
    if shutil.which("git") is None:
        yield None
        return
    root = Path(__file__).resolve().parents[2]
    baseline = subprocess.run(
        ["git", "show", f"{BASELINE_REVISION}:codd/fix/design_updater.py"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if baseline.returncode:
        # The recorded expectations still run in shallow CI and source archives.
        yield None
        return

    module_name = "_codd_design_updater_baseline"
    legacy = ModuleType(module_name)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(sys.modules, module_name, legacy)
        code = compile(baseline.stdout, f"{BASELINE_REVISION}:design_updater.py", "exec")
        exec(code, legacy.__dict__)
        yield legacy


def _response(updater, error_type, target, proposal, *, allow_delete=False):
    try:
        result = updater(
            target,
            phenomenon_text=PHENOMENON,
            analysis=PhenomenonAnalysis(),
            ai_invoke=lambda _prompt: proposal,
            allow_delete=allow_delete,
        )
    except error_type as exc:
        return str(exc), None
    return None, result


def _accepted(case_id, original, proposed, expected=None, *, allow_delete=False):
    return pytest.param(
        original, proposed, allow_delete, None, proposed if expected is None else expected,
        id=case_id,
    )


def _rejected(case_id, original, proposed, error, *, allow_delete=False):
    return pytest.param(original, proposed, allow_delete, error, None, id=case_id)


@pytest.mark.parametrize(
    ("original", "proposal", "allow_delete", "baseline_error", "expected_content"),
    [
        _accepted("ordinary-update", ORIGINAL, UPDATED),
        _accepted("unchanged", ORIGINAL, ORIGINAL),
        _accepted("edit-other-frontmatter", ORIGINAL, UPDATED.replace("title: Login", "title: Sign in")),
        _accepted("move-codd-block", ORIGINAL, _doc(CODD_BLOCK + DESIGN_HEADER + REQUIRED_ENTRIES, NEW_BODY)),
        _rejected("change-codd-scalar", ORIGINAL, UPDATED.replace("'green'", "'amber'"), TAMPER_ERROR),
        _rejected("add-codd-child", ORIGINAL, UPDATED.replace("  band:", "  kind: page\n  band:"), TAMPER_ERROR),
        _rejected("delete-codd-child", ORIGINAL, UPDATED.replace("  band: 'green'\n", ""), TAMPER_ERROR),
        _rejected(
            "reorder-codd-children", ORIGINAL,
            UPDATED.replace(CODD_BLOCK, "codd:\n  band: 'green'\n  node_id: login\n  # Keep this tooling comment.\n"),
            TAMPER_ERROR,
        ),
        _rejected("edit-indented-comment", ORIGINAL, UPDATED.replace("Keep this", "Edit this"), TAMPER_ERROR),
        _accepted("dedented-comment", ORIGINAL, UPDATED.replace(CODD_BLOCK, CODD_BLOCK + "# Document comment\n")),
        _accepted("trailing-spaces-in-codd", ORIGINAL, UPDATED.replace("'green'\n", "'green'  \n")),
        _accepted("trailing-blank-in-codd", ORIGINAL, UPDATED.replace(CODD_BLOCK, CODD_BLOCK + "\n")),
        _rejected(
            "interior-spaces-in-codd", ORIGINAL, UPDATED.replace("node_id: login", "node_id: login  "),
            TAMPER_ERROR,
        ),
        _rejected("codd-indentation", ORIGINAL, UPDATED.replace("  band:", "    band:"), TAMPER_ERROR),
        _rejected("codd-key-trailing-spaces", ORIGINAL, UPDATED.replace("codd:\n", "codd:  \n"), TAMPER_ERROR),
        _rejected("canonical-codd-removed", ORIGINAL, NO_CODD, TAMPER_ERROR),
        _rejected("canonical-codd-removed-allow-delete", ORIGINAL, NO_CODD, TAMPER_ERROR, allow_delete=True),
        _rejected("canonical-codd-added", NO_CODD, UPDATED, TAMPER_ERROR),
        _rejected("canonical-codd-renamed", ORIGINAL, UPDATED.replace("codd:", "metadata:"), TAMPER_ERROR),
        _rejected(
            "canonical-to-inline-codd", ORIGINAL,
            INLINE_CODD.replace(DESIGN_HEADER, DESIGN_HEADER + REQUIRED_ENTRIES), TAMPER_ERROR,
        ),
        _accepted("inline-codd-changed", INLINE_CODD, INLINE_CODD.replace("node_id: login", "node_id: welcome")),
        _accepted("inline-codd-added", _doc(DESIGN_HEADER), INLINE_CODD),
        _accepted("inline-codd-removed", INLINE_CODD, _doc(DESIGN_HEADER, NEW_BODY)),
        _accepted("quoted-codd-changed", QUOTED_CODD, QUOTED_CODD.replace("node_id: login", "node_id: welcome")),
        _accepted("spaced-codd-key-changed", SPACED_CODD, SPACED_CODD.replace("node_id: login", "node_id: welcome")),
        _accepted("nested-codd-changed", NESTED_CODD, NESTED_CODD.replace("node_id: login", "node_id: welcome")),
        _accepted("duplicate-codd", ORIGINAL, DUPLICATE_CODD),
        _accepted(
            "second-codd-change", DUPLICATE_CODD,
            _doc(DESIGN_HEADER + REQUIRED_ENTRIES + CODD_BLOCK + CODD_BLOCK.replace("'green'", "'amber'"), NEW_BODY),
        ),
        _accepted("literal-codd-trailing-spaces", LITERAL_CODD, LITERAL_CODD.replace("spaces  ", "spaces")),
        _accepted("anchor-codd-changed", ANCHOR_CODD, ANCHOR_CODD.replace("node_id: login", "node_id: welcome")),
        _accepted("malformed-yaml", ORIGINAL, MALFORMED_YAML),
        _rejected("missing-frontmatter-with-required-entries", ORIGINAL, NEW_BODY, SHRINK_ERROR),
        _rejected("missing-frontmatter-with-codd-only", _doc(CODD_BLOCK), NEW_BODY, TAMPER_ERROR),
        _accepted("missing-frontmatter-without-codd", _doc(DESIGN_HEADER), NEW_BODY),
        _accepted("without-original-frontmatter", BODY, NEW_BODY),
        _accepted("add-frontmatter-without-codd", BODY, NO_CODD),
        _rejected("add-frontmatter-with-codd", BODY, UPDATED, TAMPER_ERROR),
        _accepted("without-original-frontmatter-preface", BODY, PREFACE + NO_CODD),
        _accepted("body-delimiter-without-original-frontmatter", BODY, "# Login\n\n---\nMore body.\n"),
        _accepted("second-frontmatter-kept", ORIGINAL, UPDATED + _doc("codd:\n  node_id: other\n")),
        _accepted("adjacent-second-frontmatter-kept", ORIGINAL, ADJACENT_HEADERS),
        _rejected("first-frontmatter-controls-double-frontmatter", ORIGINAL, NO_CODD + UPDATED, TAMPER_ERROR),
        _accepted("whole-markdown-wrapper", ORIGINAL, f"```markdown\n{UPDATED}```\n", UPDATED),
        _accepted("whole-unlabelled-wrapper", ORIGINAL, f"```\n{UPDATED}```", UPDATED),
        _rejected("tilde-wrapper", ORIGINAL, f"~~~markdown\n{UPDATED}~~~", TAMPER_ERROR),
        _rejected("fence-with-trailing-prose", ORIGINAL, f"```markdown\n{UPDATED}```\nAfterward.\n", TAMPER_ERROR),
        _rejected("short-preface-inside-wrapper", ORIGINAL, f"```markdown\n{PREFACE}{UPDATED}```\n", TAMPER_ERROR),
        _accepted("leading-and-trailing-whitespace", ORIGINAL, f"\n \t{UPDATED} \n", UPDATED),
        _accepted("closing-delimiter-with-trailing-spaces", ORIGINAL, UPDATED.replace("---\n# Login", "---  \n# Login")),
        _accepted("closing-delimiter-at-eof", _doc(CODD_BLOCK), _doc(CODD_BLOCK, "").rstrip(), _doc(CODD_BLOCK, "")),
        _rejected("missing-closing-delimiter", ORIGINAL, UPDATED.replace("---\n# Login", "# Login"), TAMPER_ERROR),
        _rejected("long-preface", ORIGINAL, "x" * 401 + "\n\n" + UPDATED, TAMPER_ERROR),
        _rejected("multiple-paragraphs", ORIGINAL, PREFACE + "Another paragraph.\n\n" + UPDATED, TAMPER_ERROR),
        _rejected("multiple-lines", ORIGINAL, "One line.\nAnother line.\n\n" + UPDATED, TAMPER_ERROR),
        _rejected("missing-blank-line-before-frontmatter", ORIGINAL, "One line.\n" + UPDATED, TAMPER_ERROR),
        _rejected("unicode-line-separator-is-not-a-blank-line", ORIGINAL, "Updated.\u2028\n" + UPDATED, TAMPER_ERROR),
        _rejected("heading-before-frontmatter", ORIGINAL, "# Updated design\n\n" + UPDATED, TAMPER_ERROR),
        _rejected("fence-before-frontmatter", ORIGINAL, "```markdown\n\n" + UPDATED, TAMPER_ERROR),
        _rejected("quote-before-frontmatter", ORIGINAL, "> Updated design\n\n" + UPDATED, TAMPER_ERROR),
        _rejected("list-before-frontmatter", ORIGINAL, "1. Updated design\n\n" + UPDATED, TAMPER_ERROR),
        _rejected("parenthesized-list-before-frontmatter", ORIGINAL, "1) Updated design\n\n" + UPDATED, TAMPER_ERROR),
        _rejected("indented-code-before-frontmatter", ORIGINAL, "    Updated design\n\n" + UPDATED, TAMPER_ERROR),
        _rejected("preface-before-adjacent-headers", ORIGINAL, PREFACE + ADJACENT_HEADERS, TAMPER_ERROR),
        _rejected("preface-before-extra-delimiter", ORIGINAL, PREFACE + "---\n" + UPDATED, TAMPER_ERROR),
        _rejected(
            "preface-before-incomplete-frontmatter", ORIGINAL,
            PREFACE + UPDATED.replace("---\n# Login", "# Login"), TAMPER_ERROR,
        ),
        _accepted("suffix-kept", ORIGINAL, UPDATED + "\nAnything after the document stays.\n"),
        _accepted("crlf-original-with-lf-proposal", CRLF_ORIGINAL, UPDATED),
        _accepted("crlf-original-with-no-op", CRLF_ORIGINAL, ORIGINAL),
        _rejected("crlf-proposal-with-codd", CRLF_ORIGINAL, UPDATED.replace("\n", "\r\n"), TAMPER_ERROR),
        _accepted(
            "crlf-proposal-without-codd", _doc(DESIGN_HEADER),
            NO_CODD.replace("\n", "\r\n"), NO_CODD.rstrip().replace("\n", "\r\n") + "\n",
        ),
        _accepted("cr-original-with-lf-proposal", ORIGINAL.replace("\n", "\r"), UPDATED),
        _accepted("bom-retained", BOM_ORIGINAL, "\ufeff" + UPDATED),
        _rejected("bom-removed-for-canonical-codd", BOM_ORIGINAL, UPDATED, TAMPER_ERROR),
        _rejected("bom-added-for-canonical-codd", ORIGINAL, "\ufeff" + UPDATED, TAMPER_ERROR),
        _accepted("bom-removed-without-codd", "\ufeff" + NO_CODD, NO_CODD),
        _accepted("bare-suffix-rejected-but-original-proposal-allowed", BOM_ORIGINAL, PREFACE + UPDATED),
        _accepted("delete-required-with-permission", ORIGINAL, UPDATED.replace(REQUIRED_ENTRIES, ""), allow_delete=True),
        _rejected("delete-required-without-permission", ORIGINAL, UPDATED.replace(REQUIRED_ENTRIES, ""), SHRINK_ERROR),
        _rejected("empty", ORIGINAL, " \n\t ", EMPTY_ERROR),
    ],
)
def test_matches_legacy_acceptance_and_content(
    tmp_path, legacy_design_updater, original, proposal, allow_delete, baseline_error,
    expected_content,
):
    target = tmp_path / "design.md"
    target.write_bytes(original.encode("utf-8"))
    expected_original = original.replace("\r\n", "\n").replace("\r", "\n")
    if legacy_design_updater is not None:
        legacy_error, legacy = _response(
            legacy_design_updater.update_design_doc, legacy_design_updater.DesignUpdateError,
            target, proposal, allow_delete=allow_delete,
        )
        assert legacy_error == baseline_error
        if legacy is not None:
            assert legacy.original_content == expected_original
            assert legacy.proposed_content == expected_content

    actual_error, current = _response(
        update_design_doc, DesignUpdateError, target, proposal, allow_delete=allow_delete,
    )
    if baseline_error is not None:
        assert actual_error is not None
        return

    assert actual_error is None
    assert current.original_content == expected_original
    assert current.proposed_content == expected_content
    assert not current.reason
    assert current.changed == (expected_original != expected_content)
    if legacy_design_updater is not None:
        assert current.diff == legacy.diff
    apply_update(current)
    assert target.read_bytes() == (expected_content if current.changed else original).encode("utf-8")


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


@pytest.mark.parametrize(
    ("original", "expected_hash"),
    [
        pytest.param(ORIGINAL, "b4b713680ec2010360e0f67687f09a08f4cdcad56df4a188727283ad0f8b0053", id="lf"),
        pytest.param(CRLF_ORIGINAL, "b4b713680ec2010360e0f67687f09a08f4cdcad56df4a188727283ad0f8b0053", id="crlf"),
        pytest.param(BOM_ORIGINAL, "760fa5c2c4ae0a1d3e6bf021e26a523026a3281f81edc4a359ac480db1971908", id="bom"),
    ],
)
def test_first_design_prompt_matches_pre_change_bytes(
    tmp_path, legacy_design_updater, original, expected_hash,
):
    target = tmp_path / "design.md"
    target.write_bytes(original.encode("utf-8"))
    proposal = original.replace("\r\n", "\n")
    ai, prompts = _scripted_design_ai(proposal)
    update_design_doc(target, phenomenon_text=PHENOMENON, analysis=PhenomenonAnalysis(), ai_invoke=ai)

    (prompt,) = prompts
    path_line = f"\nCURRENT_DOCUMENT_PATH: {target}\n"
    assert prompt.count(path_line) == 1
    normalized = prompt.replace(path_line, f"\nCURRENT_DOCUMENT_PATH: {TARGET}\n")
    assert _prompt_hash(normalized) == expected_hash
    if legacy_design_updater is not None:
        ai, legacy_prompts = _scripted_design_ai(proposal)
        legacy_design_updater.update_design_doc(
            target, phenomenon_text=PHENOMENON, analysis=PhenomenonAnalysis(), ai_invoke=ai,
        )
        (legacy_prompt,) = legacy_prompts
        assert prompt.encode("utf-8") == legacy_prompt.encode("utf-8")


@pytest.mark.parametrize(
    "proposal",
    [
        pytest.param(PREFACE + UPDATED, id="plain"),
        pytest.param(PREFACE + UPDATED + "\nTrailing content stays.\n", id="suffix-retained"),
        pytest.param(PREFACE + UPDATED.replace("title: Login", "title: Sign in"), id="frontmatter-edit-retained"),
        pytest.param("変更を反映しました。\n\n" + UPDATED, id="japanese"),
        pytest.param("あ" * 400 + "\n\n" + UPDATED, id="unicode-length-limit"),
    ],
)
def test_short_preface_applies_same_content_as_bare_proposal(tmp_path, proposal):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    bare_proposal = proposal.split("\n\n", 1)[1]
    bare = update_design_doc(
        project / TARGET, phenomenon_text=PHENOMENON, analysis=PhenomenonAnalysis(),
        ai_invoke=lambda _prompt: bare_proposal,
    )
    ai, prompts = _scripted_design_ai(proposal)

    result = run_phenomenon_fix(
        project, PHENOMENON, ai_invoke=ai, non_interactive=True, propagate_impl=False,
    )

    assert len(prompts) == 1
    assert result.applied_paths == [TARGET], result.attempts
    (attempt,) = result.attempts
    assert attempt.update is not None
    assert attempt.update.proposed_content == bare.proposed_content
    assert attempt.update.diff == bare.diff
    assert RECOVERY_WARNING in attempt.update.reason.lower()
    assert (project / TARGET).read_bytes() == bare.proposed_content.encode("utf-8")


def test_short_preface_does_not_bypass_required_section_validation(tmp_path):
    target = tmp_path / "design.md"
    target.write_text(ORIGINAL, encoding="utf-8")
    shrunk = PREFACE + UPDATED.replace(REQUIRED_ENTRIES, "")

    with pytest.raises(DesignUpdateError, match=re.escape(SHRINK_ERROR)):
        update_design_doc(
            target, phenomenon_text=PHENOMENON, analysis=PhenomenonAnalysis(),
            ai_invoke=lambda _prompt: shrunk,
        )

    assert target.read_bytes() == ORIGINAL.encode("utf-8")


def test_cli_prints_preface_recovery_warning(tmp_path, monkeypatch):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    ai, prompts = _scripted_design_ai(PREFACE + UPDATED)
    monkeypatch.setattr(
        phenomenon_fixer, "_build_default_ai_invoke", lambda *_args, **_kwargs: ai,
    )

    result = CliRunner().invoke(main, [
        "fix", PHENOMENON, "--path", str(project), "--non-interactive",
        "--no-propagate-impl", "--no-push",
    ])

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1
    assert result.output.lower().count(RECOVERY_WARNING) == 1
    assert (project / TARGET).read_bytes() == UPDATED.encode("utf-8")


def test_dry_run_retry_only_includes_the_latest_validation_error(tmp_path):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    shrunk = ORIGINAL.replace(REQUIRED_ENTRIES, "")
    ai, prompts = _scripted_design_ai(shrunk, "", UPDATED)

    result = run_phenomenon_fix(
        project, PHENOMENON, ai_invoke=ai, non_interactive=True,
        propagate_impl=False, dry_run=True,
    )

    assert [attempt.attempt for attempt in result.attempts] == [1, 2, 3]
    first_failure, second_failure, preview = result.attempts
    assert first_failure.update is None and SHRINK_ERROR in first_failure.aborted_reason
    assert second_failure.update is None and EMPTY_ERROR in second_failure.aborted_reason
    assert preview.update is not None and preview.update.proposed_content == UPDATED
    assert not result.applied_paths
    assert (project / TARGET).read_bytes() == ORIGINAL.encode("utf-8")
    first, second, third = prompts
    assert SHRINK_ERROR not in first and EMPTY_ERROR not in first
    _assert_retry_prompt(second, 2, SHRINK_ERROR)
    assert EMPTY_ERROR not in second
    _assert_retry_prompt(third, 3, EMPTY_ERROR)
    assert SHRINK_ERROR not in third


@pytest.mark.parametrize(
    ("proposal", "diagnostic"),
    [
        pytest.param("# Explanation\n\n" + UPDATED, LEADING_TEXT_ERROR, id="heading"),
        pytest.param("```markdown\n\n" + UPDATED, LEADING_TEXT_ERROR, id="fence"),
        pytest.param("x" * 401 + "\n\n" + UPDATED, LEADING_TEXT_ERROR, id="long-preface"),
        pytest.param(PREFACE + DEEPLY_NESTED_YAML, LEADING_TEXT_ERROR, id="deeply-nested-prefaced-yaml"),
        pytest.param(NEW_BODY, MISSING_FRONTMATTER_ERROR, id="missing-frontmatter"),
        pytest.param(NO_CODD, MISSING_CODD_ERROR, id="missing-codd"),
        pytest.param(UPDATED.replace("'green'", "'amber'"), TAMPER_ERROR, id="changed-codd"),
    ],
)
def test_cli_retries_with_specific_diagnostics_without_echoing_proposal(
    tmp_path, monkeypatch, proposal, diagnostic,
):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    rejected = proposal + REJECTED_MARKER + "\n"
    ai, prompts = _scripted_design_ai(rejected, rejected, rejected)
    monkeypatch.setattr(
        phenomenon_fixer, "_build_default_ai_invoke", lambda *_args, **_kwargs: ai,
    )

    result = CliRunner().invoke(main, [
        "fix", PHENOMENON, "--path", str(project), "--non-interactive",
        "--no-propagate-impl", "--no-push",
    ])

    assert result.exit_code == 1, result.output
    assert result.output.count(diagnostic) == 3
    assert REJECTED_MARKER not in result.output
    for attempt in (1, 2, 3):
        assert f"Attempt {attempt} on {TARGET}" in result.output
    assert (project / TARGET).read_bytes() == ORIGINAL.encode("utf-8")

    first, second, third = prompts
    assert diagnostic not in first
    assert len({_prompt_hash(prompt) for prompt in prompts}) == 3
    for attempt, prompt in enumerate((second, third), start=2):
        _assert_retry_prompt(prompt, attempt, diagnostic)
        assert REJECTED_MARKER not in prompt
        assert "return only the full updated document" in prompt.lower()
        assert "preserve the original frontmatter verbatim" in prompt.lower()
        assert "omit any preface" in prompt.lower()


def test_cli_interactive_allow_delete_still_applies_after_a_retry(tmp_path, monkeypatch):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    shrunk = UPDATED.replace(REQUIRED_ENTRIES, "")
    ai, prompts = _scripted_design_ai("", shrunk)
    monkeypatch.setattr(
        phenomenon_fixer, "_build_default_ai_invoke", lambda *_args, **_kwargs: ai,
    )

    result = CliRunner().invoke(main, [
        "fix", PHENOMENON, "--path", str(project), "--allow-delete",
        "--no-propagate-impl", "--no-push",
    ], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Apply update to design/login.md?" in result.output
    assert (project / TARGET).read_bytes() == shrunk.encode("utf-8")
    first, second = prompts
    assert "ALLOW_DELETE: true" in first and "ALLOW_DELETE: true" in second
    _assert_retry_prompt(second, 2, EMPTY_ERROR)


@pytest.mark.parametrize(
    "proposal",
    [
        pytest.param("Here is\tthe updated document.\n\n" + UPDATED, id="internal-tab"),
        pytest.param(f"{PREFACE}```markdown\n{UPDATED}```\n", id="complete-code-fence"),
    ],
)
def test_short_preface_rejects_tab_and_fenced_document(tmp_path, proposal):
    target = tmp_path / "design.md"
    target.write_text(ORIGINAL, encoding="utf-8")

    with pytest.raises(DesignUpdateError, match=LEADING_TEXT_ERROR):
        update_design_doc(
            target, phenomenon_text=PHENOMENON, analysis=PhenomenonAnalysis(),
            ai_invoke=lambda _prompt: proposal,
        )

    assert target.read_bytes() == ORIGINAL.encode("utf-8")


@pytest.mark.parametrize(
    "original",
    [pytest.param(_doc(DESIGN_HEADER), id="no-codd-block"), pytest.param(BODY, id="no-frontmatter")],
)
def test_short_preface_recovery_requires_original_codd_block(tmp_path, monkeypatch, original):
    target = tmp_path / "design.md"
    target.write_text(original, encoding="utf-8")
    proposal = PREFACE + INLINE_CODD

    def unexpected_recovery(_raw):
        pytest.fail("Preface recovery ran without an original codd: block")

    monkeypatch.setattr("codd.fix.design_updater._strip_short_preface", unexpected_recovery)

    update = update_design_doc(
        target, phenomenon_text=PHENOMENON, analysis=PhenomenonAnalysis(),
        ai_invoke=lambda _prompt: proposal,
    )
    assert update.proposed_content == proposal
    assert not update.reason

    with pytest.raises(DesignUpdateError, match=re.escape(TAMPER_ERROR)):
        update_design_doc(
            target, phenomenon_text=PHENOMENON, analysis=PhenomenonAnalysis(),
            ai_invoke=lambda _prompt: UPDATED,
        )


def test_short_preface_recovery_normalizes_ending_and_keeps_body_code_fence(tmp_path):
    target = tmp_path / "design.md"
    target.write_text(ORIGINAL, encoding="utf-8")
    expected = UPDATED + "\n```text\nExample error message\n```\n"
    proposal = PREFACE + expected.removesuffix("\n") + "  "

    update = update_design_doc(
        target, phenomenon_text=PHENOMENON, analysis=PhenomenonAnalysis(),
        ai_invoke=lambda _prompt: proposal,
    )

    assert update.proposed_content == expected
    assert RECOVERY_WARNING in update.reason.lower()


def test_cli_reports_preface_discard_once_on_stderr(tmp_path, monkeypatch, capsys):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    ai, prompts = _scripted_design_ai(PREFACE + UPDATED)
    monkeypatch.setattr(
        phenomenon_fixer, "_build_default_ai_invoke", lambda *_args, **_kwargs: ai,
    )

    main.main(args=[
        "fix", PHENOMENON, "--path", str(project), "--non-interactive",
        "--no-propagate-impl", "--no-push",
    ], standalone_mode=False)

    stdout, stderr = capsys.readouterr()
    assert stderr.lower().count(RECOVERY_WARNING) == 1
    assert RECOVERY_WARNING not in stdout.lower()
    assert len(prompts) == 1
    assert (project / TARGET).read_bytes() == UPDATED.encode("utf-8")


def test_design_exception_clears_validation_feedback_before_next_retry(tmp_path):
    project = _write_project(tmp_path, {TARGET: ORIGINAL})
    shrunk = UPDATED.replace(REQUIRED_ENTRIES, "")
    failure = "scripted design invocation failed"
    ai, prompts = _scripted_design_ai(shrunk, RuntimeError(failure), UPDATED)

    def invoke(prompt):
        response = ai(prompt)
        if isinstance(response, RuntimeError):
            raise response
        return response

    result = run_phenomenon_fix(
        project, PHENOMENON, ai_invoke=invoke, non_interactive=True, propagate_impl=False,
    )

    first_failure, second_failure, success = result.attempts
    assert first_failure.update is None and SHRINK_ERROR in first_failure.aborted_reason
    assert second_failure.update is None
    assert second_failure.aborted_reason == f"design_updater error: {failure}"
    assert success.applied
    assert result.applied_paths == [TARGET]
    assert (project / TARGET).read_bytes() == UPDATED.encode("utf-8")
    first, second, third = prompts
    _assert_retry_prompt(second, 2, SHRINK_ERROR)
    assert third == first
