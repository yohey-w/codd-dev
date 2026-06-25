"""Stage-2 Axis-P Phase A1: amber/gap Finding -> amber AskItem bridge (owner-free).

This is the owner-free core of Axis-P coverage. A coverage gap discovered by a
model or structural pass (an *amber* / *gap* :class:`Finding`) is converted by a
purely *mechanical* transform into a non-blocking :class:`AskItem`
(``blocking=False``, ``status="ASK"`` -> ``RECOMMENDED_PROCEEDING`` when a
recommendation exists) and persisted into
``ProjectLexicon.coverage_decisions``. CI / merge / loop never wait on these
items; a human can batch-confirm later (Phase C), at which point a CONFIRMED
answer can be promoted to a contract (Phase B).

Boundary (rails):
- **No new-meaning judgement here.** This module only *transcribes* a finding
  into an ASK question. Deciding *which contract* a gap should become
  (gap_kind -> contract) is an owner batch decision and lives in Phase B; it is
  intentionally NOT done here.
- **owner-not-a-bottleneck**: every generated item is ``blocking=False``.
- **idempotent**: the same gap re-elicited maps to the same deterministic id, so
  ``coverage_decisions`` never accumulates duplicates.
- **respect owner decisions**: an id already ``CONFIRMED`` or ``OVERRIDDEN`` is
  never dragged back to ``ASK`` / ``RECOMMENDED_PROCEEDING``.

Generality: nothing here branches on a language / framework / domain literal.
The transform reads only generic :class:`Finding` fields.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from codd.elicit.finding import Finding
from codd.hitl_session import HitlSession
from codd.lexicon import AskItem


# Statuses that represent a settled owner decision. These are never reverted by
# a re-elicit pass — the owner's call wins over a freshly re-discovered gap.
_OWNER_SETTLED_STATUSES = frozenset({"CONFIRMED", "OVERRIDDEN"})

# Severities that represent *recall* (model/structural surfaced a possible gap),
# not a deterministically enforceable contract violation. Only these become
# owner-free AskItems; harder findings are left to the deterministic RED decider.
_AMBER_SEVERITIES = frozenset({"amber", "gap"})

_ID_PREFIX = "axis_p"

# Generic detail keys that may carry a model/structural recommendation. Read in
# order; first non-empty wins. No domain literals — purely structural field names.
_RECOMMENDED_DETAIL_KEYS = (
    "recommended_id",
    "recommended_option",
    "recommendation_id",
    "recommended",
)


def finding_to_ask_item(finding: Finding) -> AskItem:
    """Transcribe an amber/gap :class:`Finding` into a non-blocking :class:`AskItem`.

    Pure mechanical conversion: no new-meaning judgement. The id is derived
    deterministically from the finding ``kind`` + a canonical subject so the same
    gap maps to the same id across runs (idempotency anchor).
    """
    ask_id = _ask_item_id(finding)
    question = _ask_question(finding)
    recommended_id = _recommended_id_from_finding(finding)
    return AskItem(
        id=ask_id,
        question=question,
        type="select",
        options=[],
        blocking=False,
        status="ASK",
        recommended_id=recommended_id,
        # Axis-P provenance for Phase C promotion: carry the canonical gap kind +
        # subject as structured fields so promotion routes the kind without
        # re-parsing the id. The values match the tokens embedded in ``ask_id``.
        gap_kind=_canonical_token(finding.kind) or "gap",
        gap_subject=_canonical_subject(finding),
        gap_context=_gap_context_from_finding(finding),
    )


def upsert_ask_items(
    existing: list[AskItem],
    incoming: list[AskItem],
) -> list[AskItem]:
    """Merge ``incoming`` AskItems into ``existing`` by id (idempotent).

    - New ids are appended (existing order preserved, then new ids in input order).
    - A duplicate incoming id refreshes a *pending* (ASK / RECOMMENDED_PROCEEDING)
      entry but never an owner-settled (CONFIRMED / OVERRIDDEN) one.
    - Repeated incoming ids collapse to a single entry.
    """
    by_id: dict[str, AskItem] = {}
    order: list[str] = []
    for item in existing:
        if item.id in by_id:
            continue
        by_id[item.id] = item
        order.append(item.id)

    for item in incoming:
        prior = by_id.get(item.id)
        if prior is None:
            by_id[item.id] = item
            order.append(item.id)
            continue
        # Respect owner decisions: never revert a settled status to ASK.
        if prior.status in _OWNER_SETTLED_STATUSES:
            continue
        # Pending entry: keep the latest recall transcription, but carry forward
        # any recommendation / gap provenance already recorded so we never lose it.
        if item.recommended_id is None and prior.recommended_id is not None:
            item.recommended_id = prior.recommended_id
        if item.gap_kind is None and prior.gap_kind is not None:
            item.gap_kind = prior.gap_kind
        if item.gap_subject is None and prior.gap_subject is not None:
            item.gap_subject = prior.gap_subject
        if not item.gap_context and prior.gap_context:
            item.gap_context = prior.gap_context
        by_id[item.id] = item

    return [by_id[item_id] for item_id in order]


def bridge_findings_to_lexicon(
    findings: list[Finding],
    lexicon_path: str | Path,
) -> list[AskItem]:
    """Owner-free flow: amber findings -> AskItems -> coverage_decisions.

    Steps (all mechanical, CI never blocks):
      1. Keep only amber/gap recall findings (hard findings stay with the
         deterministic RED decider).
      2. Convert each to a non-blocking AskItem (:func:`finding_to_ask_item`).
      3. Load prior ``coverage_decisions`` and :func:`upsert_ask_items`
         (dedupe by id; owner CONFIRMED/OVERRIDDEN preserved).
      4. :meth:`HitlSession.proceed_with_recommended` so non-blocking items with a
         recommendation advance to ``RECOMMENDED_PROCEEDING`` (never wait on CI).
      5. Persist via :meth:`HitlSession.save_to_lexicon`.

    Returns the AskItems produced for *this* set of findings (post-merge state),
    in input order.
    """
    path = Path(lexicon_path)
    new_items = [
        finding_to_ask_item(finding)
        for finding in findings
        if _is_amber_recall(finding)
    ]

    session = HitlSession()
    if path.exists():
        session.load_from_lexicon(path)

    merged = upsert_ask_items(session.ask_items, new_items)
    session.ask_items = merged

    # Non-blocking ASK items with a recommendation -> RECOMMENDED_PROCEEDING.
    # Owner-settled statuses are untouched by proceed_with_recommended (it only
    # advances status == "ASK"); items without a recommendation stay ASK.
    if new_items:
        session.proceed_with_recommended()
        session.save_to_lexicon(path)
    elif not path.exists():
        # Nothing to bridge and no prior file: do not create an empty lexicon.
        return []

    new_ids = [item.id for item in new_items]
    current = {item.id: item for item in session.ask_items}
    return [current[item_id] for item_id in new_ids if item_id in current]


# ---------------------------------------------------------------------------
# internals (pure, deterministic)
# ---------------------------------------------------------------------------

def _is_amber_recall(finding: Finding) -> bool:
    return str(getattr(finding, "severity", "")).strip().lower() in _AMBER_SEVERITIES


def _ask_item_id(finding: Finding) -> str:
    kind = _canonical_token(finding.kind) or "gap"
    subject = _canonical_subject(finding)
    return f"{_ID_PREFIX}.{kind}.{subject}"


def _canonical_subject(finding: Finding) -> str:
    """Derive a stable subject token for the AskItem id.

    Preference order (all generic, no domain literals):
      1. an explicit subject-ish detail field,
      2. the suffix of the finding id after the last ``:`` (the existing finding
         id convention, e.g. ``missing_journey_for_actor:operator``),
      3. the whole finding id.
    The result is lower-cased with non-alphanumerics collapsed to ``_`` so the
    same gap always yields the same token.
    """
    details = finding.details if isinstance(finding.details, dict) else {}
    for key in ("canonical_subject", "subject", "actor", "node", "resource", "name"):
        value = details.get(key)
        token = _canonical_token(value)
        if token:
            return token

    raw_id = str(finding.id or "")
    if ":" in raw_id:
        tail = raw_id.rsplit(":", 1)[1]
        token = _canonical_token(tail)
        if token:
            return token

    return _canonical_token(raw_id) or "subject"


def _canonical_token(value: Any) -> str:
    if not isinstance(value, str):
        if value is None:
            return ""
        value = str(value)
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _ask_question(finding: Finding) -> str:
    if finding.question and finding.question.strip():
        return finding.question.strip()
    if finding.rationale and finding.rationale.strip():
        return finding.rationale.strip()
    if finding.name and finding.name.strip():
        return finding.name.strip()
    return f"Coverage gap detected: {finding.kind}"


def _recommended_id_from_finding(finding: Finding) -> str | None:
    details = finding.details if isinstance(finding.details, dict) else {}
    for key in _RECOMMENDED_DETAIL_KEYS:
        value = details.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# Generic detail keys carrying a routing-relevant contract payload. A finding may
# pre-shape the exact contract fields (e.g. the consuming capability + required
# flag a missing_producer must declare); Phase C merges this verbatim into the
# promoted entry. No domain literals — purely structural field names, so the
# contract shape is data-driven (the finding emitter owns it, not the promoter).
_CONTEXT_DETAIL_KEYS = ("contract_entry", "contract_payload", "gap_context")


def _gap_context_from_finding(finding: Finding) -> dict[str, Any]:
    details = finding.details if isinstance(finding.details, dict) else {}
    for key in _CONTEXT_DETAIL_KEYS:
        value = details.get(key)
        if isinstance(value, dict) and value:
            return {str(k): v for k, v in value.items()}
    return {}


__all__ = [
    "bridge_findings_to_lexicon",
    "finding_to_ask_item",
    "upsert_ask_items",
]
