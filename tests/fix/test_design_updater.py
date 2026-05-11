"""Unit tests for codd.fix.design_updater."""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.fix.design_updater import (
    DesignUpdateError,
    apply_update,
    update_design_doc,
)
from codd.fix.phenomenon_parser import PhenomenonAnalysis


def _doc_with_journeys(extra: str = "") -> str:
    return (
        "---\n"
        "title: Login\n"
        "description: login form\n"
        "user_journeys:\n"
        "  - id: u1\n"
        "    description: sign in\n"
        "  - id: u2\n"
        "    description: forgot password\n"
        "acceptance_criteria:\n"
        "  - id: c1\n"
        "    description: invalid creds show clear error\n"
        "codd:\n"
        "  node_id: auth_login\n"
        "  band: green\n"
        "---\n"
        "# Login\n"
        "Body text.\n"
        f"{extra}"
    )


def test_update_writes_diff_and_changed_flag(tmp_path):
    target = tmp_path / "auth_login.md"
    target.write_text(_doc_with_journeys(), encoding="utf-8")
    updated = _doc_with_journeys("Additional note about error messages.\n")

    update = update_design_doc(
        target,
        phenomenon_text="error wording",
        analysis=PhenomenonAnalysis(intent="improvement"),
        ai_invoke=lambda _p: updated,
    )
    assert update.changed
    assert "Additional note" in update.proposed_content
    assert update.diff


def test_no_op_when_llm_returns_unchanged_content(tmp_path):
    target = tmp_path / "auth_login.md"
    original = _doc_with_journeys()
    target.write_text(original, encoding="utf-8")

    update = update_design_doc(
        target,
        phenomenon_text="x",
        analysis=PhenomenonAnalysis(intent="improvement"),
        ai_invoke=lambda _p: original,
    )
    assert not update.changed
    assert update.is_no_op()


def test_required_section_shrink_is_rejected(tmp_path):
    target = tmp_path / "auth_login.md"
    target.write_text(_doc_with_journeys(), encoding="utf-8")

    # Remove one user_journey — should fail without allow_delete.
    shrunk = _doc_with_journeys().replace(
        "  - id: u2\n    description: forgot password\n",
        "",
    )
    with pytest.raises(DesignUpdateError):
        update_design_doc(
            target,
            phenomenon_text="x",
            analysis=PhenomenonAnalysis(),
            ai_invoke=lambda _p: shrunk,
            allow_delete=False,
        )


def test_required_section_shrink_allowed_with_flag(tmp_path):
    target = tmp_path / "auth_login.md"
    target.write_text(_doc_with_journeys(), encoding="utf-8")
    shrunk = _doc_with_journeys().replace(
        "  - id: u2\n    description: forgot password\n",
        "",
    )
    update = update_design_doc(
        target,
        phenomenon_text="x",
        analysis=PhenomenonAnalysis(),
        ai_invoke=lambda _p: shrunk,
        allow_delete=True,
    )
    assert update.changed


def test_codd_metadata_modification_is_rejected(tmp_path):
    target = tmp_path / "auth_login.md"
    target.write_text(_doc_with_journeys(), encoding="utf-8")
    tampered = _doc_with_journeys().replace("band: green", "band: amber")
    with pytest.raises(DesignUpdateError):
        update_design_doc(
            target,
            phenomenon_text="x",
            analysis=PhenomenonAnalysis(),
            ai_invoke=lambda _p: tampered,
        )


def test_empty_llm_output_raises(tmp_path):
    target = tmp_path / "auth_login.md"
    target.write_text(_doc_with_journeys(), encoding="utf-8")
    with pytest.raises(DesignUpdateError):
        update_design_doc(
            target,
            phenomenon_text="x",
            analysis=PhenomenonAnalysis(),
            ai_invoke=lambda _p: "   ",
        )


def test_apply_update_writes_to_disk(tmp_path):
    target = tmp_path / "auth_login.md"
    target.write_text(_doc_with_journeys(), encoding="utf-8")
    new_body = _doc_with_journeys("Appended.\n")
    update = update_design_doc(
        target,
        phenomenon_text="x",
        analysis=PhenomenonAnalysis(),
        ai_invoke=lambda _p: new_body,
    )
    apply_update(update)
    assert "Appended." in target.read_text(encoding="utf-8")


def test_apply_update_noop_does_not_change_mtime(tmp_path):
    target = tmp_path / "auth_login.md"
    original = _doc_with_journeys()
    target.write_text(original, encoding="utf-8")
    update = update_design_doc(
        target,
        phenomenon_text="x",
        analysis=PhenomenonAnalysis(),
        ai_invoke=lambda _p: original,
    )
    apply_update(update)
    assert target.read_text(encoding="utf-8") == original


def test_code_fence_is_stripped(tmp_path):
    target = tmp_path / "auth_login.md"
    target.write_text(_doc_with_journeys(), encoding="utf-8")
    fenced = "```markdown\n" + _doc_with_journeys("Appended.\n") + "```"
    update = update_design_doc(
        target,
        phenomenon_text="x",
        analysis=PhenomenonAnalysis(),
        ai_invoke=lambda _p: fenced,
    )
    assert not update.proposed_content.startswith("```")
    assert "Appended." in update.proposed_content
