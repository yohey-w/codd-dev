"""Tests for design-token drift fix strategy."""

from __future__ import annotations

from codd.coherence_engine import DriftEvent
from codd.fixup_drift_strategies import get_strategy, list_registered_kinds
from codd.fixup_drift_strategies.design_token_drift import DesignTokenDriftFixStrategy


def _event(payload: dict | None = None) -> DriftEvent:
    return DriftEvent(
        source_artifact="design_md",
        target_artifact="implementation",
        change_type="modified",
        payload=payload
        or {
            "token_name": "primary",
            "old_value": "--colorPrimary",
            "new_value": "--color-primary",
            "file": "src/styles.css",
        },
        severity="amber",
        fix_strategy="hitl",
        kind="design_token_drift",
    )


def test_design_token_strategy_registered(tmp_path):
    assert "design_token_drift" in list_registered_kinds()
    assert isinstance(get_strategy("design_token_drift", tmp_path), DesignTokenDriftFixStrategy)


def test_propose_case_normalization(tmp_path):
    strategy = DesignTokenDriftFixStrategy(tmp_path)

    proposal = strategy.propose(_event())[0]

    assert proposal.kind == "design_token_drift"
    assert proposal.file_path == "src/styles.css"
    assert proposal.can_auto_apply is True
    assert "--colorPrimary" in proposal.diff
    assert "--color-primary" in proposal.diff


def test_propose_value_change(tmp_path):
    strategy = DesignTokenDriftFixStrategy(tmp_path)

    proposal = strategy.propose(
        _event(
            {
                "token": "#ff0000",
                "actual_value": "#ff0000",
                "expected_value": "#0000ff",
                "file": "App.tsx",
            }
        )
    )[0]

    assert proposal.file_path == "App.tsx"
    assert proposal.can_auto_apply is False
    assert "Requires human review" in proposal.description


def test_propose_missing_values_requires_human_review(tmp_path):
    strategy = DesignTokenDriftFixStrategy(tmp_path)

    proposal = strategy.propose(_event({"file": "App.tsx"}))[0]

    assert proposal.can_auto_apply is False


def test_is_case_normalization_same(tmp_path):
    strategy = DesignTokenDriftFixStrategy(tmp_path)

    assert strategy._is_case_normalization("--colorPrimary", "--color-primary") is True


def test_is_case_normalization_different(tmp_path):
    strategy = DesignTokenDriftFixStrategy(tmp_path)

    assert strategy._is_case_normalization("#ff0000", "#0000ff") is False


def test_apply_case_normalization_writes_file(tmp_path):
    styles = tmp_path / "src" / "styles.css"
    styles.parent.mkdir()
    styles.write_text(".button { color: var(--colorPrimary); }\n", encoding="utf-8")
    strategy = DesignTokenDriftFixStrategy(tmp_path)
    proposal = strategy.propose(_event())[0]

    assert strategy.apply(proposal) is True
    assert styles.read_text(encoding="utf-8") == ".button { color: var(--color-primary); }\n"


def test_apply_value_change_returns_false(tmp_path):
    strategy = DesignTokenDriftFixStrategy(tmp_path)
    proposal = strategy.propose(
        _event(
            {
                "token": "#ff0000",
                "actual_value": "#ff0000",
                "expected_value": "#0000ff",
                "file": "App.tsx",
            }
        )
    )[0]

    assert strategy.apply(proposal) is False
