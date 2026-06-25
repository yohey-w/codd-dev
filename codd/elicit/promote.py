"""Stage-2 Axis-P Phase C1: promote CONFIRMED coverage decisions to contracts.

Phase A turned coverage gaps into owner-free amber ``AskItem``s persisted in
``ProjectLexicon.coverage_decisions``. A human later *batch-confirms* the ones
that are real (``status == CONFIRMED``). This module closes the owner-free loop:
it promotes **only those CONFIRMED decisions** into *explicit* contract entries
in a design-doc's ``frontmatter.codd`` block, so the existing deterministic
checks (resource_flow_coherence, negative_space, user_journey_coherence, ...)
pick them up and go **red** on a real violation.

Rails (the owner-free design's core invariants):

* **red only via owner-CONFIRMED.** Only ``status == CONFIRMED`` decisions are
  promoted. ``ASK`` / ``RECOMMENDED_PROCEEDING`` (the owner has not confirmed)
  and ``OVERRIDDEN`` (the owner rejected the recommendation) are never promoted.
  Model confidence or a not-yet-answered ask is therefore *never* turned into a
  red — anti-false-red.
* **RECOMMENDED-default routing, owner-overridable.** ``gap_kind -> contract_key``
  comes from :mod:`codd.elicit.routing` (a recommendation a project overrides
  via ``codd.yaml`` ``axis_p.gap_routing``). An unknown gap kind routes nowhere
  and is **left as amber residue** (not promoted) — new meaning is never
  hard-coded into a contract.
* **idempotent.** Each promoted entry is tagged ``source: axis_p_confirmed`` and
  keyed by its gap identity; re-running upserts in place (no duplicate
  contracts), so promotion can run every loop safely.
* **generality / backward compatibility.** No project/framework/language literal
  is read. Existing frontmatter contracts and existing checks are untouched; a
  project with no CONFIRMED axis-P decisions gets a no-op.

The promoter does NOT build the DAG or write ``.codd/dag.json`` — it edits the
design-doc source files directly via :mod:`codd.frontmatter`, leaving the DAG /
check pipeline to read the promoted contracts on its next run.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from codd.elicit.routing import (
    PROMOTION_SOURCE,
    resolve_routing,
    route_gap_kind,
    split_contract_key,
)
from codd.frontmatter import parse_frontmatter
from codd.lexicon import AskItem, LEXICON_FILENAME, ProjectLexicon


# A settled owner CONFIRM is the *only* promotable status (see module rails).
_PROMOTABLE_STATUS = "CONFIRMED"


@dataclass
class PromotionResult:
    """Outcome of a :func:`promote_confirmed_to_contract` run."""

    promoted: list[dict[str, Any]] = field(default_factory=list)
    skipped_unknown_kind: list[dict[str, Any]] = field(default_factory=list)
    skipped_not_confirmed: list[str] = field(default_factory=list)
    changed_docs: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changed_docs)


def promote_confirmed_to_contract(
    project_root: str | Path,
    *,
    lexicon_path: str | Path | None = None,
    codd_config: dict[str, Any] | None = None,
    routing_override: dict[str, Any] | None = None,
    target_design_doc: str | Path | None = None,
) -> PromotionResult:
    """Promote CONFIRMED coverage decisions into explicit design-doc contracts.

    Steps (all deterministic, none block):

    1. Load ``coverage_decisions`` and keep only ``status == CONFIRMED``.
    2. For each, recover the gap kind + subject (structured ``gap_kind`` /
       ``gap_subject`` fields, falling back to the ``axis_p.<kind>.<subject>``
       id encoding for decisions persisted before those fields existed).
    3. Route the kind via :mod:`codd.elicit.routing` (RECOMMENDED default +
       ``codd.yaml`` / call overrides). Unknown kind -> recorded as
       ``skipped_unknown_kind`` and left amber (NOT promoted).
    4. Upsert a contract entry under each routed key in the target design-doc's
       ``frontmatter.codd`` block, tagged ``source: axis_p_confirmed`` (idempotent).

    Returns a :class:`PromotionResult` describing what was promoted / skipped.
    Writes the design doc only when something changed.
    """
    root = Path(project_root)
    lex_path = Path(lexicon_path) if lexicon_path is not None else _default_lexicon_path(root)
    result = PromotionResult()

    decisions = _load_coverage_decisions(lex_path)
    confirmed = [d for d in decisions if (d.status or "").strip() == _PROMOTABLE_STATUS]
    for decision in decisions:
        if (decision.status or "").strip() != _PROMOTABLE_STATUS:
            result.skipped_not_confirmed.append(decision.id)
    if not confirmed:
        return result

    rules = resolve_routing(codd_config, override=routing_override)

    # Group promotable entries by (top_key, sub_key) so each design-doc section is
    # upserted once. {(top, sub): [entry, ...]}
    pending: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for decision in confirmed:
        gap_kind, gap_subject = _recover_gap_identity(decision)
        targets = route_gap_kind(gap_kind, rules)
        if not targets:
            # Unknown / unrouted kind: safe side — leave amber residue, do not
            # invent a contract for a meaning the routing table does not know.
            result.skipped_unknown_kind.append(
                {"id": decision.id, "gap_kind": gap_kind, "gap_subject": gap_subject}
            )
            continue
        for contract_key in targets:
            top_key, sub_key = split_contract_key(contract_key)
            if not top_key:
                continue
            entry = _build_contract_entry(decision, gap_kind, gap_subject)
            pending.setdefault((top_key, sub_key), []).append(entry)
            result.promoted.append(
                {
                    "id": decision.id,
                    "gap_kind": gap_kind,
                    "gap_subject": gap_subject,
                    "contract_key": contract_key,
                }
            )

    if not pending:
        return result

    target_path = _resolve_target_doc(root, codd_config, target_design_doc)
    if target_path is None:
        # No design doc to promote into. Nothing is written; the decisions stay
        # confirmed-but-unpromoted (a visible no-op, never a silent false-green).
        result.promoted.clear()
        return result

    changed = _upsert_into_design_doc(target_path, pending)
    if changed:
        try:
            rel = str(target_path.relative_to(root))
        except ValueError:
            rel = str(target_path)
        result.changed_docs.append(rel)
    return result


# ---------------------------------------------------------------------------
# gap identity recovery (structured field first, id fallback)
# ---------------------------------------------------------------------------

#: AskItem id encoding produced by gap_to_ask: ``axis_p.<kind>.<subject>``.
_ID_PREFIX = "axis_p"


def _recover_gap_identity(decision: AskItem) -> tuple[str | None, str | None]:
    """Return ``(gap_kind, gap_subject)`` for a coverage decision.

    Prefers the structured ``gap_kind`` / ``gap_subject`` fields (Phase A now
    records them). Falls back to parsing the ``axis_p.<kind>.<subject>`` id for
    decisions persisted before those fields existed (backward compatibility).
    """
    gap_kind = (decision.gap_kind or "").strip() or None
    gap_subject = (decision.gap_subject or "").strip() or None
    if gap_kind and gap_subject:
        return gap_kind, gap_subject

    parsed_kind, parsed_subject = _parse_id(decision.id)
    return gap_kind or parsed_kind, gap_subject or parsed_subject


def _parse_id(ask_id: str | None) -> tuple[str | None, str | None]:
    """Parse ``axis_p.<kind>.<subject>`` into ``(kind, subject)``.

    Both ``<kind>`` and ``<subject>`` are canonical tokens (no ``.``), so the id
    splits into exactly three dot-separated parts. A non-axis-P id (no prefix /
    wrong shape) yields ``(None, None)`` — the decision then routes nowhere and
    is left as amber residue (safe side).
    """
    parts = str(ask_id or "").split(".")
    if len(parts) != 3 or parts[0] != _ID_PREFIX:
        return None, None
    kind = parts[1].strip() or None
    subject = parts[2].strip() or None
    return kind, subject


# ---------------------------------------------------------------------------
# contract entry construction (idempotent identity + traceable provenance)
# ---------------------------------------------------------------------------

def _build_contract_entry(
    decision: AskItem, gap_kind: str | None, gap_subject: str | None
) -> dict[str, Any]:
    """Build the contract entry promoted from one CONFIRMED decision.

    The entry is intentionally minimal and generic: a stable ``id`` (the gap's
    canonical identity, so re-promotion is idempotent), the resolved
    ``gap_subject`` as the contract's subject (``resource`` / ``name``), the
    answered value if any, and the ``source: axis_p_confirmed`` provenance tag
    so the entry is traceable and recognizable on re-run. Downstream checks read
    whichever of these fields they understand; unknown fields are ignored
    (additive, backward-compatible).
    """
    entry: dict[str, Any] = {
        "id": _entry_id(gap_kind, gap_subject, decision.id),
        "source": PROMOTION_SOURCE,
        "source_decision_id": decision.id,
        "gap_kind": gap_kind,
    }
    if gap_subject:
        # Provide the subject under the generic keys the various checks read so
        # one entry shape works across contract types without per-kind branching:
        #   resource_contracts -> ``resource``; user_journeys/others -> ``name``.
        entry["resource"] = gap_subject
        entry["name"] = gap_subject
    answer = (decision.answer or "").strip()
    if answer:
        entry["confirmed_answer"] = answer
    # Data-driven contract shape: the originating finding may have pre-shaped the
    # exact contract fields the check needs (e.g. a required consumer for a
    # missing_producer). Merge that verbatim so promotion is generic — the entry
    # shape is owned by the finding emitter, never hard-coded per kind here. The
    # provenance keys above are not overwritten by context.
    context = decision.gap_context if isinstance(decision.gap_context, dict) else {}
    for key, value in context.items():
        if key in ("source", "source_decision_id"):
            continue
        entry[key] = deepcopy(value)
    return entry


def _entry_id(gap_kind: str | None, gap_subject: str | None, decision_id: str) -> str:
    kind = (gap_kind or "gap").strip() or "gap"
    subject = (gap_subject or "").strip()
    if subject:
        return f"{PROMOTION_SOURCE}.{kind}.{subject}"
    # No subject available: fall back to the decision id for a stable identity.
    return f"{PROMOTION_SOURCE}.{kind}.{decision_id}"


# ---------------------------------------------------------------------------
# design-doc frontmatter upsert (idempotent, codd-block scoped)
# ---------------------------------------------------------------------------

def _upsert_into_design_doc(
    target_path: Path,
    pending: dict[tuple[str, str | None], list[dict[str, Any]]],
) -> bool:
    """Upsert contract entries into ``target_path``'s ``frontmatter.codd`` block.

    Returns True when the file content changed. Idempotent: an entry whose ``id``
    already exists under the target key (a prior promotion) is updated in place,
    not duplicated. The ``codd`` block is created if absent. Non-axis-P sibling
    entries in the same list are preserved.
    """
    original = target_path.read_text(encoding="utf-8")
    parsed = parse_frontmatter(original)
    frontmatter: dict[str, Any] = deepcopy(parsed.mapping) if parsed.has_block else {}
    codd_block = frontmatter.get("codd")
    if not isinstance(codd_block, dict):
        codd_block = {}

    for (top_key, sub_key), entries in pending.items():
        if sub_key is None:
            _upsert_list(codd_block, top_key, entries)
        else:
            _upsert_nested(codd_block, top_key, sub_key, entries)

    frontmatter["codd"] = codd_block
    new_content = _render_with_frontmatter(original, parsed, frontmatter)
    if new_content == original:
        return False
    target_path.write_text(new_content, encoding="utf-8")
    return True


def _upsert_list(
    container: dict[str, Any], key: str, entries: list[dict[str, Any]]
) -> None:
    existing = container.get(key)
    target_list: list[Any] = list(existing) if isinstance(existing, list) else []
    for entry in entries:
        _upsert_entry(target_list, entry)
    container[key] = target_list


def _upsert_nested(
    container: dict[str, Any], top_key: str, sub_key: str, entries: list[dict[str, Any]]
) -> None:
    """Upsert into ``container[top_key][sub_key]`` (a list).

    Two shapes are supported by the contract vocabulary:
      * ``negative_space.forbidden_evidence`` — ``negative_space`` is a *mapping*
        whose ``forbidden_evidence`` is a list.
      * ``user_journeys.expected_outcomes`` — ``user_journeys`` is a *list* of
        journeys; the outcomes attach to (a synthetic axis-P) journey entry's
        ``expected_outcomes`` list, so promotion never silently drops them.
    """
    top_value = container.get(top_key)
    if isinstance(top_value, list) or top_value is None and _is_list_top(top_key):
        # list-of-entries top key: attach the sub-list to a dedicated axis-P entry
        journeys: list[Any] = list(top_value) if isinstance(top_value, list) else []
        anchor = _find_axis_p_anchor(journeys)
        if anchor is None:
            anchor = {"id": f"{PROMOTION_SOURCE}.{top_key}", "source": PROMOTION_SOURCE}
            journeys.append(anchor)
        sub_list = anchor.get(sub_key)
        sub_list = list(sub_list) if isinstance(sub_list, list) else []
        for entry in entries:
            _upsert_entry(sub_list, entry)
        anchor[sub_key] = sub_list
        container[top_key] = journeys
        return

    # mapping top key (e.g. negative_space): its sub_key is a list.
    mapping = dict(top_value) if isinstance(top_value, dict) else {}
    sub_list = mapping.get(sub_key)
    sub_list = list(sub_list) if isinstance(sub_list, list) else []
    for entry in entries:
        _upsert_entry(sub_list, entry)
    mapping[sub_key] = sub_list
    container[top_key] = mapping


def _is_list_top(top_key: str) -> bool:
    # user_journeys is the only list-shaped dotted top key in the default table.
    return top_key == "user_journeys"


def _find_axis_p_anchor(entries: list[Any]) -> dict[str, Any] | None:
    for entry in entries:
        if isinstance(entry, dict) and entry.get("source") == PROMOTION_SOURCE:
            return entry
    return None


def _upsert_entry(target_list: list[Any], entry: dict[str, Any]) -> None:
    """Insert ``entry`` or update the existing one with the same ``id`` in place."""
    entry_id = entry.get("id")
    if entry_id is not None:
        for index, existing in enumerate(target_list):
            if isinstance(existing, dict) and existing.get("id") == entry_id:
                target_list[index] = entry
                return
    target_list.append(entry)


def _render_with_frontmatter(
    original: str, parsed: Any, frontmatter: dict[str, Any]
) -> str:
    """Re-render the document with an updated frontmatter mapping.

    Preserves the original body byte-for-byte. When the original had no
    frontmatter block, a new ``---`` block is prepended ahead of the whole file.
    """
    rendered = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    block = f"---\n{rendered}---\n"
    if getattr(parsed, "has_block", False):
        body = parsed.body
        return block + body
    # No prior block: prepend one; keep the original content as the body.
    return block + original


# ---------------------------------------------------------------------------
# lexicon / target-doc resolution
# ---------------------------------------------------------------------------

def _default_lexicon_path(root: Path) -> Path:
    for name in ("codd", ".codd"):
        candidate = root / name / LEXICON_FILENAME
        if candidate.exists():
            return candidate
    # Fall back to the conventional location even if absent (load returns []).
    return root / "codd" / LEXICON_FILENAME


def _load_coverage_decisions(lexicon_path: Path) -> list[AskItem]:
    if not lexicon_path.exists():
        return []
    data = yaml.safe_load(lexicon_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return []
    return ProjectLexicon(data).coverage_decisions


def _resolve_target_doc(
    root: Path,
    codd_config: dict[str, Any] | None,
    target_design_doc: str | Path | None,
) -> Path | None:
    """Resolve the design doc to promote into (deterministic).

    Precedence:
      1. explicit ``target_design_doc`` argument,
      2. ``codd.yaml`` ``axis_p.promote_target`` (a project-relative path),
      3. the single design doc if exactly one exists,
      4. the lexicographically-first design doc (stable, deterministic).
    Returns ``None`` when no design doc can be found (caller makes it a no-op).
    """
    if target_design_doc is not None:
        candidate = Path(target_design_doc)
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate if candidate.exists() else None

    configured = _configured_target(codd_config)
    if configured:
        candidate = root / configured
        if candidate.exists():
            return candidate

    docs = _discover_design_docs(root, codd_config)
    if not docs:
        return None
    return docs[0]


def _configured_target(codd_config: dict[str, Any] | None) -> str | None:
    if not isinstance(codd_config, dict):
        return None
    namespace = codd_config.get("axis_p")
    if not isinstance(namespace, dict):
        return None
    value = namespace.get("promote_target")
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _discover_design_docs(root: Path, codd_config: dict[str, Any] | None) -> list[Path]:
    """Find design-doc ``.md`` files carrying CoDD frontmatter, sorted by path.

    Uses the configured ``scan.doc_dirs`` (defaults to ``docs/``). A markdown
    file qualifies when its frontmatter has a ``codd`` block (the design-doc
    marker). No DAG build / no ``.codd/dag.json`` write.
    """
    doc_dirs = ["docs"]
    if isinstance(codd_config, dict):
        scan = codd_config.get("scan")
        if isinstance(scan, dict) and isinstance(scan.get("doc_dirs"), list):
            doc_dirs = [str(d).rstrip("/") for d in scan["doc_dirs"] if str(d).strip()]

    found: list[Path] = []
    for doc_dir in doc_dirs:
        base = root / doc_dir
        if not base.exists():
            continue
        for md_path in sorted(base.rglob("*.md")):
            try:
                text = md_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            parsed = parse_frontmatter(text)
            if parsed.has_block and isinstance(parsed.mapping.get("codd"), dict):
                found.append(md_path)
    return sorted(found, key=lambda p: str(p))


__all__ = [
    "PromotionResult",
    "promote_confirmed_to_contract",
]
