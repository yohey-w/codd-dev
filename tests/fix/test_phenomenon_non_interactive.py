"""--non-interactive behavior tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd.fix.phenomenon_fixer import run_phenomenon_fix
from codd.fix.interactive_prompt import InteractivePrompt, PromptAbort


def _project(tmp_path: Path, body: str = "login form body.") -> Path:
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
        "---\n"
        "title: Login\n"
        "description: login form\n"
        "user_journeys:\n"
        "  - id: u1\n"
        "    description: sign in\n"
        "acceptance_criteria:\n"
        "  - id: c1\n"
        "    description: clear errors\n"
        "codd:\n"
        "  node_id: login\n"
        "  band: green\n"
        f"---\n# Login\n\n{body}\n",
        encoding="utf-8",
    )
    return tmp_path


def _ai(parser: dict, updated_body: str, risk: dict | None = None):
    risk_payload = json.dumps(risk or {"risky": False, "categories": []})
    responses = [
        json.dumps(parser),
        "{}",
        updated_body,
        risk_payload,
    ]
    iter_ = iter(responses)
    return lambda _p: next(iter_, "{}")


def test_non_interactive_top1_picks_highest(tmp_path):
    """When candidates are ambiguous but on_ambiguity=top1, we proceed."""
    project = _project(tmp_path)
    updated = (project / "design/login.md").read_text(encoding="utf-8").replace(
        "login form body.",
        "login form body.\nAdded.",
    )
    result = run_phenomenon_fix(
        project,
        "login wording",
        ai_invoke=_ai(
            {"intent": "improvement", "subject_terms": ["login"],
             "lexicon_hits": ["login"], "ambiguity_score": 0.05},
            updated,
        ),
        non_interactive=True,
        on_ambiguity="top1",
    )
    assert not result.aborted
    assert result.applied_paths


def test_non_interactive_abort_blocks_ambiguous(tmp_path):
    project = _project(tmp_path)
    result = run_phenomenon_fix(
        project,
        "fix it",
        ai_invoke=lambda _p: json.dumps({
            "intent": "unknown",
            "subject_terms": ["it"],
            "lexicon_hits": [],
            "ambiguity_score": 0.95,
        }),
        non_interactive=True,
        on_ambiguity="abort",
    )
    assert result.aborted


def test_non_interactive_risky_change_uses_safe_default(tmp_path):
    """Non-interactive + risky diff → safe default rejects."""
    project = _project(tmp_path)
    updated = (project / "design/login.md").read_text(encoding="utf-8").replace(
        "login form body.",
        "login form body.\nDROP TABLE users;",
    )
    result = run_phenomenon_fix(
        project,
        "login wording",
        ai_invoke=_ai(
            {"intent": "improvement", "subject_terms": ["login"],
             "lexicon_hits": ["login"], "ambiguity_score": 0.05},
            updated,
            risk={"risky": True, "categories": ["schema_migration"],
                  "summary": "DDL detected"},
        ),
        non_interactive=True,
        on_ambiguity="top1",
    )
    # In non-interactive + risky, the safe default rejects → not applied.
    assert not result.applied_paths
