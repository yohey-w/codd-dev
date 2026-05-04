"""Tests for the URL drift fix strategy."""

from __future__ import annotations

from codd.coherence_engine import DriftEvent
from codd.fixup_drift_strategies import FixProposal, list_registered_kinds
from codd.fixup_drift_strategies.url_drift import UrlDriftFixStrategy


def _event(payload=None, severity="amber") -> DriftEvent:
    return DriftEvent(
        source_artifact="design_doc",
        target_artifact="implementation",
        change_type="modified",
        payload=payload
        if payload is not None
        else {
            "old_url": "/pricing",
            "new_url": "/plans",
        },
        severity=severity,
        fix_strategy="hitl",
        kind="url_drift",
    )


def test_url_drift_strategy_registered():
    assert "url_drift" in list_registered_kinds()


def test_propose_returns_proposal(tmp_path):
    proposal = UrlDriftFixStrategy(tmp_path).propose(_event())[0]

    assert isinstance(proposal, FixProposal)
    assert proposal.kind == "url_drift"
    assert proposal.file_path == "design_doc"
    assert "Human verification required" in proposal.description
    assert proposal.severity == "amber"


def test_proposal_hitl_only(tmp_path):
    proposal = UrlDriftFixStrategy(tmp_path).propose(_event())[0]

    assert proposal.can_auto_apply is False


def test_proposal_diff_contains_urls(tmp_path):
    proposal = UrlDriftFixStrategy(tmp_path).propose(
        _event({"old_url": "https://example.com/v1", "new_url": "https://example.com/v2"})
    )[0]

    assert "-https://example.com/v1" in proposal.diff
    assert "+https://example.com/v2" in proposal.diff


def test_apply_returns_false(tmp_path):
    proposal = UrlDriftFixStrategy(tmp_path).propose(_event())[0]

    assert UrlDriftFixStrategy(tmp_path).apply(proposal) is False


def test_propose_unknown_payload(tmp_path):
    proposal = UrlDriftFixStrategy(tmp_path).propose(_event({}))[0]

    assert "-<unknown>" in proposal.diff
    assert "+<unknown>" in proposal.diff
    assert proposal.can_auto_apply is False
