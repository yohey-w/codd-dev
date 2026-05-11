"""Unit tests for codd.fix.candidate_selector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd.dag import DAG, Node
from codd.fix.candidate_selector import select_candidates
from codd.fix.phenomenon_parser import PhenomenonAnalysis


def _make_dag(tmp_path: Path, docs: dict[str, tuple[str, str]]) -> DAG:
    """Create a DAG with design_doc nodes whose files we materialize."""
    dag = DAG()
    for node_id, (kind, body) in docs.items():
        rel_path = node_id
        (tmp_path / rel_path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel_path).write_text(body, encoding="utf-8")
        dag.add_node(Node(id=node_id, kind=kind, path=rel_path, attributes={}))
    return dag


def test_no_design_docs_yields_empty_selection(tmp_path):
    dag = DAG()
    analysis = PhenomenonAnalysis(intent="improvement", subject_terms=["x"])
    result = select_candidates(analysis, dag=dag, project_root=tmp_path)
    assert result.candidates == []
    assert "no design_doc" in result.fallback_reason


def test_tier1_lexicon_match_scores_highest(tmp_path):
    dag = _make_dag(
        tmp_path,
        {
            "auth/login.md": ("design_doc", "# Login\nlogin error wording"),
            "billing/pay.md": ("design_doc", "# Pay\nstripe checkout flow"),
        },
    )
    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["login"],
        lexicon_hits=["login"],
    )
    result = select_candidates(analysis, dag=dag, project_root=tmp_path)
    assert result.candidates
    top = result.candidates[0]
    assert top.node_id == "auth/login.md"
    assert top.tier_1_score >= 1.0


def test_common_kind_included_by_default(tmp_path):
    dag = _make_dag(
        tmp_path,
        {
            "shared/errors.md": ("common", "# Errors\nlogin failure messages"),
        },
    )
    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])
    result = select_candidates(analysis, dag=dag, project_root=tmp_path)
    assert any(c.node_id == "shared/errors.md" for c in result.candidates)


def test_common_kind_excluded_when_disabled(tmp_path):
    dag = _make_dag(
        tmp_path,
        {"shared/errors.md": ("common", "login failure messages")},
    )
    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])
    result = select_candidates(
        analysis,
        dag=dag,
        project_root=tmp_path,
        include_common=False,
    )
    assert not result.candidates


def test_clear_winner_flag(tmp_path):
    dag = _make_dag(
        tmp_path,
        {
            "auth/login.md": ("design_doc", "login login login"),
            "billing/pay.md": ("design_doc", "stripe payment"),
        },
    )
    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])
    result = select_candidates(analysis, dag=dag, project_root=tmp_path)
    assert result.candidates[0].node_id == "auth/login.md"
    assert result.is_clear_winner


def test_tier2_llm_invoked_when_provided(tmp_path):
    dag = _make_dag(
        tmp_path,
        {
            "auth/login.md": ("design_doc", "login flow"),
            "auth/oauth.md": ("design_doc", "external IdP integration"),
        },
    )
    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["external login"],
        lexicon_hits=[],
    )

    def fake_ai(_prompt: str) -> str:
        return json.dumps({
            "scores": {
                "auth/oauth.md": {"score": 0.85, "reason": "external IdP matches"},
            }
        })

    result = select_candidates(
        analysis,
        dag=dag,
        project_root=tmp_path,
        ai_invoke=fake_ai,
    )
    ids = [c.node_id for c in result.candidates]
    assert "auth/oauth.md" in ids


def test_tier2_llm_failure_does_not_crash(tmp_path):
    dag = _make_dag(
        tmp_path,
        {"auth/login.md": ("design_doc", "login flow")},
    )
    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])

    def bad_ai(_prompt: str) -> str:
        raise RuntimeError("ai unavailable")

    result = select_candidates(
        analysis,
        dag=dag,
        project_root=tmp_path,
        ai_invoke=bad_ai,
    )
    # Tier 1 still produced a candidate even though tier 2 errored.
    assert result.candidates
    assert result.candidates[0].node_id == "auth/login.md"


def test_max_candidates_cap(tmp_path):
    docs = {f"d/{i}.md": ("design_doc", "login") for i in range(10)}
    dag = _make_dag(tmp_path, docs)
    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])
    result = select_candidates(
        analysis,
        dag=dag,
        project_root=tmp_path,
        max_candidates=3,
    )
    assert len(result.candidates) == 3
