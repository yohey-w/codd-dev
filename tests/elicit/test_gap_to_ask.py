"""Stage-2 Axis-P Phase A1: amber/gap Finding -> amber AskItem bridge (owner-free).

A coverage gap discovered by a model/structural pass becomes a non-blocking
AskItem (status ASK -> RECOMMENDED_PROCEEDING when a recommendation exists),
persisted into ProjectLexicon.coverage_decisions. CI/merge/loop never wait on
these. Re-eliciting the same gap is idempotent; owner decisions
(CONFIRMED / OVERRIDDEN) are never reverted to ASK.
"""

from __future__ import annotations

from pathlib import Path

from codd.elicit.finding import Finding
from codd.elicit.gap_to_ask import (
    bridge_findings_to_lexicon,
    finding_to_ask_item,
    upsert_ask_items,
)
from codd.lexicon import AskItem, AskOption, load_lexicon
from codd.hitl_session import HitlSession


def _amber_finding(
    *,
    kind: str = "missing_journey_for_actor",
    subject: str = "Operator",
    recommended: str | None = None,
) -> Finding:
    details: dict[str, object] = {"actor": subject, "dimension": "process_user_journey"}
    if recommended is not None:
        details["recommended_id"] = recommended
    return Finding(
        id=f"{kind}:{subject.lower()}",
        kind=kind,
        severity="amber",
        name="Missing user journey for actor",
        question=f"What user_journey should cover actor '{subject}'?",
        details=details,
        rationale=f"Actor '{subject}' has no declared user_journey.",
    )


# ---------------------------------------------------------------------------
# finding_to_ask_item: pure mechanical conversion
# ---------------------------------------------------------------------------

def test_amber_finding_becomes_non_blocking_ask_item() -> None:
    item = finding_to_ask_item(_amber_finding())

    assert isinstance(item, AskItem)
    assert item.blocking is False  # CI/merge/loop must never block
    assert item.status == "ASK"
    assert "Operator" in item.question


def test_ask_item_id_is_deterministic_from_kind_and_subject() -> None:
    a = finding_to_ask_item(_amber_finding(kind="missing_producer", subject="Order Total"))
    b = finding_to_ask_item(_amber_finding(kind="missing_producer", subject="Order Total"))

    assert a.id == b.id
    assert "axis_p" in a.id
    assert "missing_producer" in a.id
    # canonical subject: lower-cased, whitespace collapsed to a stable token
    assert "order_total" in a.id


def test_recommended_finding_carries_recommended_id() -> None:
    item = finding_to_ask_item(_amber_finding(recommended="declare_journey"))

    assert item.recommended_id == "declare_journey"


# ---------------------------------------------------------------------------
# upsert_ask_items: dedupe by id, owner decisions preserved
# ---------------------------------------------------------------------------

def test_upsert_dedupes_by_id() -> None:
    item = finding_to_ask_item(_amber_finding())

    merged = upsert_ask_items([item], [item, item])

    assert len(merged) == 1
    assert merged[0].id == item.id


def test_upsert_keeps_unrelated_existing_decisions() -> None:
    existing = AskItem(id="unrelated.q", question="Keep me?", blocking=False)
    new = finding_to_ask_item(_amber_finding())

    merged = upsert_ask_items([existing], [new])

    ids = {entry.id for entry in merged}
    assert "unrelated.q" in ids
    assert new.id in ids


def test_upsert_does_not_revert_confirmed_owner_decision() -> None:
    item = finding_to_ask_item(_amber_finding())
    confirmed = AskItem(
        id=item.id,
        question=item.question,
        blocking=False,
        status="CONFIRMED",
        answer="declare_journey",
    )

    merged = upsert_ask_items([confirmed], [item])

    assert len(merged) == 1
    assert merged[0].status == "CONFIRMED"
    assert merged[0].answer == "declare_journey"


def test_upsert_does_not_revert_overridden_owner_decision() -> None:
    item = finding_to_ask_item(_amber_finding())
    overridden = AskItem(id=item.id, question=item.question, blocking=False, status="OVERRIDDEN")

    merged = upsert_ask_items([overridden], [item])

    assert merged[0].status == "OVERRIDDEN"


# ---------------------------------------------------------------------------
# bridge_findings_to_lexicon: end-to-end persistence (owner-free)
# ---------------------------------------------------------------------------

def test_bridge_persists_amber_finding_and_proceeds(tmp_path: Path) -> None:
    lexicon_path = tmp_path / "project_lexicon.yaml"

    items = bridge_findings_to_lexicon(
        [_amber_finding(recommended="declare_journey")],
        lexicon_path,
    )

    assert len(items) == 1
    # proceeded because a recommendation exists -> never waits on CI
    assert items[0].status == "RECOMMENDED_PROCEEDING"
    assert items[0].blocking is False

    persisted = load_lexicon(tmp_path).coverage_decisions
    assert len(persisted) == 1
    assert persisted[0].status == "RECOMMENDED_PROCEEDING"
    assert persisted[0].blocking is False


def test_bridge_without_recommendation_stays_ask_non_blocking(tmp_path: Path) -> None:
    lexicon_path = tmp_path / "project_lexicon.yaml"

    items = bridge_findings_to_lexicon([_amber_finding()], lexicon_path)

    # no recommendation -> remains ASK, but still non-blocking (CI never waits)
    assert items[0].status == "ASK"
    assert items[0].blocking is False


def test_bridge_is_idempotent_across_reelicit(tmp_path: Path) -> None:
    lexicon_path = tmp_path / "project_lexicon.yaml"

    bridge_findings_to_lexicon([_amber_finding(recommended="declare_journey")], lexicon_path)
    bridge_findings_to_lexicon([_amber_finding(recommended="declare_journey")], lexicon_path)

    persisted = load_lexicon(tmp_path).coverage_decisions
    assert len(persisted) == 1  # no duplicate on re-run


def test_bridge_respects_prior_confirmed_decision(tmp_path: Path) -> None:
    lexicon_path = tmp_path / "project_lexicon.yaml"

    # Owner already confirmed this gap in a previous batch.
    item = finding_to_ask_item(_amber_finding())
    session = HitlSession(
        [AskItem(id=item.id, question=item.question, blocking=False, status="CONFIRMED", answer="declare_journey")]
    )
    session.save_to_lexicon(lexicon_path)

    # Re-eliciting the same gap must not drag it back to ASK/RECOMMENDED_PROCEEDING.
    bridge_findings_to_lexicon([_amber_finding(recommended="declare_journey")], lexicon_path)

    persisted = load_lexicon(tmp_path).coverage_decisions
    assert len(persisted) == 1
    assert persisted[0].status == "CONFIRMED"
    assert persisted[0].answer == "declare_journey"


def test_bridge_skips_non_amber_findings(tmp_path: Path) -> None:
    lexicon_path = tmp_path / "project_lexicon.yaml"
    critical = Finding(
        id="hard:1",
        kind="contract_violation",
        severity="critical",
        question="Hard contract violated?",
    )

    items = bridge_findings_to_lexicon([critical], lexicon_path)

    # Only amber/gap recall becomes an owner-free AskItem; hard findings are
    # left to the deterministic RED decider (Phase B / existing gates).
    assert items == []
    assert not lexicon_path.exists() or load_lexicon(tmp_path).coverage_decisions == []
