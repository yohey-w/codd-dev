"""Tests for lexicon violation fix strategy."""

from __future__ import annotations

from codd.coherence_engine import DriftEvent
from codd.fixup_drift_strategies import get_strategy, list_registered_kinds
from codd.fixup_drift_strategies.lexicon_violation import LexiconViolationFixStrategy


def _event(payload: dict | None = None) -> DriftEvent:
    return DriftEvent(
        source_artifact="lexicon",
        target_artifact="implementation",
        change_type="modified",
        payload=payload
        or {
            "term": "AuthSvc",
            "violation_type": "unknown_convention",
            "location": "docs/auth.md",
        },
        severity="red",
        fix_strategy="auto",
        kind="lexicon_violation",
    )


def test_lexicon_strategy_registered(tmp_path):
    assert "lexicon_violation" in list_registered_kinds()
    assert isinstance(get_strategy("lexicon_violation", tmp_path), LexiconViolationFixStrategy)


def test_propose_returns_proposal(tmp_path):
    strategy = LexiconViolationFixStrategy(tmp_path)

    proposal = strategy.propose(_event())[0]

    assert proposal.kind == "lexicon_violation"
    assert proposal.file_path == "docs/auth.md"
    assert "AuthSvc" in proposal.diff
    assert "unknown_convention" in proposal.description


def test_proposal_hitl_only(tmp_path):
    strategy = LexiconViolationFixStrategy(tmp_path)

    proposal = strategy.propose(_event())[0]

    assert proposal.can_auto_apply is False
    assert "Requires human review" in proposal.description


def test_apply_returns_false(tmp_path):
    strategy = LexiconViolationFixStrategy(tmp_path)
    proposal = strategy.propose(_event())[0]

    assert strategy.apply(proposal) is False


def test_adapter_payload_names_are_supported(tmp_path):
    strategy = LexiconViolationFixStrategy(tmp_path)

    proposal = strategy.propose(
        _event(
            {
                "rule": "invalid_node_id",
                "location": "docs/nodes.md",
                "actual": "bad node",
            }
        )
    )[0]

    assert proposal.file_path == "docs/nodes.md"
    assert "bad node" in proposal.diff
    assert "invalid_node_id" in proposal.description
