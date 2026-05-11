"""Integration tests for codd.fix.phenomenon_fixer.run_phenomenon_fix."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Callable

import pytest

from codd.fix.interactive_prompt import InteractivePrompt
from codd.fix.phenomenon_fixer import run_phenomenon_fix


def _write_project(tmp_path: Path, design_docs: dict[str, str]) -> Path:
    """Create a minimal CoDD project rooted at tmp_path."""
    codd_dir = tmp_path / ".codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    (codd_dir / "codd.yaml").write_text(
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

    design_dir = tmp_path / "design"
    design_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, body in design_docs.items():
        full = tmp_path / rel_path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(body, encoding="utf-8")

    return tmp_path


def _doc(title: str, description: str, body_marker: str = "") -> str:
    return (
        f"---\n"
        f"title: {title}\n"
        f"description: {description}\n"
        f"user_journeys:\n"
        f"  - id: u1\n"
        f"    description: primary flow\n"
        f"acceptance_criteria:\n"
        f"  - id: c1\n"
        f"    description: works correctly\n"
        f"codd:\n"
        f"  node_id: {title.lower().replace(' ', '_')}\n"
        f"  band: green\n"
        f"---\n"
        f"# {title}\n\n"
        f"{description} body. {body_marker}\n"
    )


def _scripted_ai(responses: list[str]) -> Callable[[str], str]:
    """Return an ai_invoke that returns the next pre-canned response on each call."""
    iter_ = iter(responses)

    def invoke(_prompt: str) -> str:
        try:
            return next(iter_)
        except StopIteration:
            return "{}"

    return invoke


def test_empty_phenomenon_aborts(tmp_path):
    project = _write_project(tmp_path, {"design/login.md": _doc("Login", "login form")})
    result = run_phenomenon_fix(project, "")
    assert result.aborted
    assert "empty" in result.abort_reason


def test_no_candidates_aborts_gracefully(tmp_path):
    project = _write_project(
        tmp_path,
        {"design/billing.md": _doc("Billing", "stripe checkout")},
    )
    parser_response = json.dumps({
        "intent": "improvement",
        "subject_terms": ["login"],
        "lexicon_hits": ["login"],
        "ambiguity_score": 0.1,
        "acceptance_signal": "",
    })
    result = run_phenomenon_fix(
        project,
        "login is broken",
        ai_invoke=_scripted_ai([parser_response, "{}"]),
        non_interactive=True,
        on_ambiguity="top1",
    )
    assert result.aborted
    assert result.analysis is not None
    assert result.analysis.intent == "improvement"


def test_happy_path_applies_update(tmp_path, monkeypatch):
    project = _write_project(
        tmp_path,
        {"design/login.md": _doc("Login", "login form")},
    )
    original = (project / "design/login.md").read_text(encoding="utf-8")
    updated = original.replace(
        "login form body.",
        "login form body.\nAdded: clearer error wording.",
    )

    parser_response = json.dumps({
        "intent": "improvement",
        "subject_terms": ["login"],
        "lexicon_hits": ["login"],
        "ambiguity_score": 0.05,
        "acceptance_signal": "",
    })

    ai = _scripted_ai([
        parser_response,        # parser
        "{}",                   # candidate_selector tier2
        updated,                # design_updater
        json.dumps({"risky": False, "categories": [], "summary": ""}),  # risk_classifier
    ])

    result = run_phenomenon_fix(
        project,
        "login error wording is unclear",
        ai_invoke=ai,
        non_interactive=True,
        on_ambiguity="top1",
    )

    assert not result.aborted, result.abort_reason
    assert result.applied_paths, result.attempts
    assert "design/login.md" in result.applied_paths[0]
    assert "Added: clearer error wording" in (project / "design/login.md").read_text(encoding="utf-8")


def test_dry_run_does_not_modify_file(tmp_path):
    project = _write_project(
        tmp_path,
        {"design/login.md": _doc("Login", "login form")},
    )
    original = (project / "design/login.md").read_text(encoding="utf-8")
    updated = original.replace("login form body.", "login form body.\nNew note.")
    parser_response = json.dumps({
        "intent": "improvement",
        "subject_terms": ["login"],
        "lexicon_hits": ["login"],
        "ambiguity_score": 0.05,
    })
    ai = _scripted_ai([
        parser_response,
        "{}",
        updated,
        json.dumps({"risky": False, "categories": []}),
    ])
    result = run_phenomenon_fix(
        project,
        "login form copy is unclear",
        ai_invoke=ai,
        non_interactive=True,
        on_ambiguity="top1",
        dry_run=True,
    )
    assert result.dry_run
    assert not result.applied_paths
    # file untouched
    assert (project / "design/login.md").read_text(encoding="utf-8") == original


def test_ambiguous_phenomenon_in_non_interactive_abort(tmp_path):
    project = _write_project(
        tmp_path,
        {"design/login.md": _doc("Login", "login form")},
    )
    parser_response = json.dumps({
        "intent": "unknown",
        "subject_terms": ["it"],
        "lexicon_hits": [],
        "ambiguity_score": 0.9,
    })
    result = run_phenomenon_fix(
        project,
        "fix it please",
        ai_invoke=_scripted_ai([parser_response]),
        non_interactive=True,
        on_ambiguity="abort",
    )
    assert result.aborted
    assert "ambiguous" in result.abort_reason.lower() or "clarification" in result.abort_reason.lower()
