"""--dry-run behavior tests for PHENOMENON mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd.fix.phenomenon_fixer import run_phenomenon_fix


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".codd").mkdir()
    (tmp_path / ".codd" / "codd.yaml").write_text(
        "scan:\n  source_dirs: []\n"
        "dag:\n"
        "  design_doc_patterns:\n"
        "    - 'design/**/*.md'\n"
        "  impl_file_patterns: []\n"
        "  test_file_patterns: []\n"
        "  scan_exclude_patterns:\n"
        "    - '.codd/**'\n"
        "    - 'tests/**'\n",
        encoding="utf-8",
    )
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "login.md").write_text(
        "---\ntitle: Login\ndescription: login form\n"
        "user_journeys:\n  - id: u1\n    description: sign in\n"
        "acceptance_criteria:\n  - id: c1\n    description: ok\n"
        "codd:\n  node_id: login\n  band: green\n"
        "---\n# Login\n\nlogin form body.\n",
        encoding="utf-8",
    )
    return tmp_path


def _ai_chain(parser: dict, updated: str, risky: bool = False):
    responses = [
        json.dumps(parser),
        "{}",
        updated,
        json.dumps({"risky": risky, "categories": [], "summary": ""}),
    ]
    iter_ = iter(responses)
    return lambda _p: next(iter_, "{}")


def test_dry_run_does_not_touch_file(tmp_path):
    project = _project(tmp_path)
    target = project / "design/login.md"
    original = target.read_text(encoding="utf-8")
    new_body = original.replace("login form body.", "login form body.\nAdded.")

    result = run_phenomenon_fix(
        project,
        "login wording",
        ai_invoke=_ai_chain(
            {"intent": "improvement", "subject_terms": ["login"],
             "lexicon_hits": ["login"], "ambiguity_score": 0.05},
            new_body,
        ),
        non_interactive=True,
        on_ambiguity="top1",
        dry_run=True,
    )
    assert result.dry_run
    assert not result.applied_paths
    assert target.read_text(encoding="utf-8") == original


def test_dry_run_still_produces_diff(tmp_path):
    project = _project(tmp_path)
    target = project / "design/login.md"
    original = target.read_text(encoding="utf-8")
    new_body = original.replace("login form body.", "login form body.\nAdded.")

    result = run_phenomenon_fix(
        project,
        "login wording",
        ai_invoke=_ai_chain(
            {"intent": "improvement", "subject_terms": ["login"],
             "lexicon_hits": ["login"], "ambiguity_score": 0.05},
            new_body,
        ),
        non_interactive=True,
        on_ambiguity="top1",
        dry_run=True,
    )
    # We should have a diff captured in the attempt result even though
    # nothing was written.
    assert result.attempts
    last = result.attempts[-1]
    assert last.update is not None
    assert last.update.diff
