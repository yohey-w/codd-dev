"""Select relevant design_doc candidates for a PHENOMENON analysis."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from codd.dag import DAG, Node
from codd.fix.phenomenon_parser import PhenomenonAnalysis
from codd.fix.templates_loader import load_template

AiInvoke = Callable[[str], str]


@dataclass
class Candidate:
    """A scored design document candidate."""

    node_id: str
    path: str
    score: float
    tier_1_score: float = 0.0
    tier_2_score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    kind: str = "design_doc"

    def to_display(self) -> str:
        reason = "; ".join(self.reasons) or "(no reason)"
        return f"{self.node_id} (score={self.score:.2f}) — {reason}"


@dataclass
class CandidateSelection:
    """Result of candidate_selector.select()."""

    candidates: list[Candidate] = field(default_factory=list)
    is_clear_winner: bool = False
    top_margin: float = 0.0
    fallback_reason: str = ""


def select_candidates(
    analysis: PhenomenonAnalysis,
    *,
    dag: DAG,
    project_root: Path,
    ai_invoke: AiInvoke | None = None,
    max_candidates: int = 5,
    score_threshold: float = 0.15,
    tier2_weight: float = 0.6,
    include_common: bool = True,
) -> CandidateSelection:
    """Score design_doc / common nodes against the phenomenon analysis.

    Tier 1 (lexicon-driven, exact substring match): score += 1.0 per hit.
    Tier 2 (semantic, LLM-driven): 0.0-1.0 per candidate, weighted by
    tier2_weight. Tier 2 is skipped when ai_invoke is None.
    """
    nodes = _collect_design_nodes(dag, include_common=include_common)
    if not nodes:
        return CandidateSelection(
            candidates=[],
            fallback_reason="no design_doc nodes in DAG",
        )

    tier1: dict[str, tuple[float, list[str]]] = {}
    for node in nodes:
        score, reasons = _tier1_score(node, analysis, project_root)
        if score > 0.0:
            tier1[node.id] = (score, reasons)

    tier2: dict[str, tuple[float, list[str]]] = {}
    if ai_invoke is not None and analysis.subject_terms:
        tier2 = _tier2_score(
            nodes=nodes,
            analysis=analysis,
            ai_invoke=ai_invoke,
            project_root=project_root,
        )

    scored: list[Candidate] = []
    candidate_ids = set(tier1) | set(tier2)
    for node_id in candidate_ids:
        node = next((n for n in nodes if n.id == node_id), None)
        if node is None:
            continue
        t1, t1_reasons = tier1.get(node_id, (0.0, []))
        t2, t2_reasons = tier2.get(node_id, (0.0, []))
        combined = t1 + tier2_weight * t2
        if combined <= 0.0:
            continue
        scored.append(
            Candidate(
                node_id=node.id,
                path=node.path or node.id,
                score=combined,
                tier_1_score=t1,
                tier_2_score=t2,
                reasons=t1_reasons + t2_reasons,
                kind=node.kind,
            )
        )

    scored.sort(key=lambda c: c.score, reverse=True)
    top = scored[: max(1, max_candidates)]

    if not top:
        return CandidateSelection(
            candidates=[],
            fallback_reason="no candidate matched lexicon or semantic search",
        )

    is_clear = False
    margin = 0.0
    if len(top) == 1:
        is_clear = True
    else:
        margin = top[0].score - top[1].score
        is_clear = margin > score_threshold

    return CandidateSelection(
        candidates=top,
        is_clear_winner=is_clear,
        top_margin=margin,
    )


def _collect_design_nodes(dag: DAG, *, include_common: bool) -> list[Node]:
    kinds = {"design_doc"}
    if include_common:
        kinds.add("common")
    return [node for node in dag.nodes.values() if node.kind in kinds]


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|[぀-ヿ一-鿿]+")


def _tier1_score(
    node: Node,
    analysis: PhenomenonAnalysis,
    project_root: Path,
) -> tuple[float, list[str]]:
    """Lexicon-driven exact match against frontmatter description and body."""
    text = _read_node_text(node, project_root)
    if not text:
        return 0.0, []

    text_lower = text.lower()
    score = 0.0
    reasons: list[str] = []

    for lex in analysis.lexicon_hits:
        key = lex.strip().lower()
        if not key:
            continue
        if key in text_lower:
            score += 1.0
            reasons.append(f"lexicon hit: {lex}")

    for term in analysis.subject_terms:
        key = term.strip().lower()
        if not key or len(key) < 2:
            continue
        if key in text_lower:
            score += 0.25
            reasons.append(f"subject term: {term}")

    return score, reasons


def _read_node_text(node: Node, project_root: Path) -> str:
    if not node.path:
        return ""
    path = project_root / node.path
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _tier2_score(
    *,
    nodes: list[Node],
    analysis: PhenomenonAnalysis,
    ai_invoke: AiInvoke,
    project_root: Path,
) -> dict[str, tuple[float, list[str]]]:
    summaries = _node_summaries(nodes, project_root)
    if not summaries:
        return {}

    prompt = _build_tier2_prompt(analysis, summaries)
    try:
        raw = ai_invoke(prompt)
    except Exception:  # noqa: BLE001
        return {}

    payload = _extract_json_object(raw)
    if not isinstance(payload, dict):
        return {}

    raw_scores = payload.get("scores")
    if not isinstance(raw_scores, dict):
        return {}

    out: dict[str, tuple[float, list[str]]] = {}
    for node_id, entry in raw_scores.items():
        score, reason = _coerce_tier2_entry(entry)
        if score > 0.0:
            out[str(node_id)] = (score, [f"semantic: {reason}" if reason else "semantic match"])
    return out


def _node_summaries(nodes: Iterable[Node], project_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in nodes:
        text = _read_node_text(node, project_root)
        if not text:
            continue
        summary = _summarize_doc(text)
        if summary:
            out[node.id] = summary
    return out


def _summarize_doc(text: str, *, max_len: int = 360) -> str:
    """Extract a short summary: frontmatter description or first paragraph."""
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        body = text[fm_match.end():]
        desc_match = re.search(r"^description:\s*(.+)$", fm_text, re.MULTILINE)
        if desc_match:
            description = desc_match.group(1).strip().strip('"').strip("'")
            if description:
                return description[:max_len]
    else:
        body = text

    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("```"):
            return stripped[:max_len]
    return ""


def _build_tier2_prompt(analysis: PhenomenonAnalysis, summaries: dict[str, str]) -> str:
    summaries_block = "\n".join(
        f"- {node_id}: {summary}" for node_id, summary in summaries.items()
    )
    analysis_block = json.dumps(analysis.to_dict(), ensure_ascii=False)
    return (
        "You are CoDD's semantic matcher. Given a phenomenon analysis and a\n"
        "list of design document summaries, score each summary 0.0-1.0 by\n"
        "how likely it is the right place to make a change. Return JSON only.\n\n"
        "PHENOMENON_ANALYSIS:\n"
        f"{analysis_block}\n\n"
        "DESIGN_DOC_SUMMARIES:\n"
        f"{summaries_block}\n\n"
        "Return JSON of shape:\n"
        '{"scores": {"<node_id>": {"score": 0.0, "reason": "<short>"}}}\n'
        "Omit nodes with score 0. Include at most 8 entries.\n"
        "JSON OUTPUT:\n"
    )


def _coerce_tier2_entry(entry: Any) -> tuple[float, str]:
    if isinstance(entry, (int, float)):
        return max(0.0, min(1.0, float(entry))), ""
    if isinstance(entry, dict):
        try:
            score = float(entry.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        reason = str(entry.get("reason", "") or "").strip()
        return max(0.0, min(1.0, score)), reason[:160]
    return 0.0, ""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    text = raw.strip()
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
