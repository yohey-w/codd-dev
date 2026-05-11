"""LLM-driven phenomenon parser for codd fix [PHENOMENON]."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from codd.fix.templates_loader import load_template, render_template

AiInvoke = Callable[[str], str]


@dataclass
class PhenomenonAnalysis:
    """Structured interpretation of a phenomenon string."""

    intent: str = "unknown"
    subject_terms: list[str] = field(default_factory=list)
    lexicon_hits: list[str] = field(default_factory=list)
    ambiguity_score: float = 1.0
    acceptance_signal: str = ""
    raw_response: str = ""

    def is_ambiguous(self, threshold: float = 0.6) -> bool:
        return self.ambiguity_score >= threshold

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "subject_terms": list(self.subject_terms),
            "lexicon_hits": list(self.lexicon_hits),
            "ambiguity_score": self.ambiguity_score,
            "acceptance_signal": self.acceptance_signal,
        }


VALID_INTENTS = {"improvement", "bugfix", "new_feature", "clarification", "unknown"}


def parse_phenomenon(
    phenomenon_text: str,
    *,
    ai_invoke: AiInvoke,
    lexicon_context: str = "",
    design_summaries: dict[str, str] | None = None,
    template_path: Path | None = None,
) -> PhenomenonAnalysis:
    """Parse a phenomenon string into a structured analysis via LLM.

    Falls back to ambiguity_score=1.0 + intent="unknown" when the LLM
    is unavailable or returns malformed output.
    """
    if not phenomenon_text or not phenomenon_text.strip():
        return PhenomenonAnalysis()

    summaries_text = _format_summaries(design_summaries or {})
    template = load_template("phenomenon_parse.txt", override=template_path)
    prompt = render_template(
        template,
        phenomenon_text=phenomenon_text.strip(),
        lexicon_context=(lexicon_context or "(none)").strip(),
        design_summaries=summaries_text,
    )

    try:
        raw = ai_invoke(prompt)
    except Exception:  # noqa: BLE001 — LLM failures must not crash CLI
        return PhenomenonAnalysis(raw_response="<ai_invoke_failed>")

    return _coerce_analysis(raw)


def _format_summaries(summaries: dict[str, str]) -> str:
    if not summaries:
        return "(none)"
    lines = []
    for node_id, summary in summaries.items():
        summary = (summary or "").strip().replace("\n", " ")
        if len(summary) > 240:
            summary = summary[:237] + "..."
        lines.append(f"- {node_id}: {summary}")
    return "\n".join(lines)


def _coerce_analysis(raw: str) -> PhenomenonAnalysis:
    payload = _extract_json(raw)
    if payload is None:
        return PhenomenonAnalysis(raw_response=raw)

    intent = str(payload.get("intent", "unknown")).strip().lower()
    if intent not in VALID_INTENTS:
        intent = "unknown"

    subject_terms = _string_list(payload.get("subject_terms"))
    lexicon_hits = _string_list(payload.get("lexicon_hits"))
    try:
        score = float(payload.get("ambiguity_score", 1.0))
    except (TypeError, ValueError):
        score = 1.0
    score = max(0.0, min(1.0, score))

    acceptance = str(payload.get("acceptance_signal", "") or "").strip()
    if len(acceptance) > 280:
        acceptance = acceptance[:277] + "..."

    return PhenomenonAnalysis(
        intent=intent,
        subject_terms=subject_terms,
        lexicon_hits=lexicon_hits,
        ambiguity_score=score,
        acceptance_signal=acceptance,
        raw_response=raw,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned:
            out.append(cleaned)
    return out


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json(raw: str) -> dict[str, Any] | None:
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
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
