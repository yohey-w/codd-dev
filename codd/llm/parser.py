"""Robust parser for LLM-derived consideration JSON."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from codd.deployment.providers.llm_consideration import (
    Consideration as DerivedConsideration,
    VerificationStrategy,
)


LOGGER = logging.getLogger(__name__)


class LlmOutputParser:
    """Parse LLM output while keeping valid entries from mixed-quality JSON."""

    def parse(self, raw_output: str) -> list[DerivedConsideration]:
        """Return valid considerations from raw JSON or fenced JSON output."""

        try:
            payload = json.loads(_strip_json_fence(raw_output))
        except json.JSONDecodeError as exc:
            LOGGER.warning("Skipping LLM output: invalid JSON: %s", exc)
            return []

        entries = _entries(payload)
        if entries is None:
            LOGGER.warning("Skipping LLM output: expected a JSON array or considerations object")
            return []

        parsed: list[DerivedConsideration] = []
        for index, entry in enumerate(entries):
            consideration = _parse_entry(entry, index)
            if consideration is not None:
                parsed.append(consideration)
        return parsed


def _entries(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping) and isinstance(payload.get("considerations"), list):
        return payload["considerations"]
    return None


def _parse_entry(entry: Any, index: int) -> DerivedConsideration | None:
    if not isinstance(entry, Mapping):
        LOGGER.warning("Skipping LLM consideration at index %s: entry must be an object", index)
        return None

    item_id = str(entry.get("id") or "").strip()
    description = str(entry.get("description") or "").strip()
    if not item_id or not description:
        LOGGER.warning("Skipping LLM consideration at index %s: id and description are required", index)
        return None

    domain_hints = entry.get("domain_hints", [])
    if not isinstance(domain_hints, list):
        LOGGER.warning("Skipping LLM consideration %s: domain_hints must be a list", item_id)
        return None

    return DerivedConsideration(
        id=item_id,
        description=description,
        domain_hints=[str(item) for item in domain_hints],
        verification_strategy=_parse_strategy(entry.get("verification_strategy")),
        approval_status=_approval_status(entry.get("approval_status")),
    )


def _parse_strategy(payload: Any) -> VerificationStrategy | None:
    if not isinstance(payload, Mapping):
        return None
    engine = str(payload.get("engine") or "").strip()
    if not engine:
        return None
    required_capabilities = payload.get("required_capabilities", [])
    if not isinstance(required_capabilities, list):
        required_capabilities = [required_capabilities]
    return VerificationStrategy(
        engine=engine,
        layer=str(payload.get("layer") or ""),
        parallelizable=bool(payload.get("parallelizable", False)),
        reason_for_choice=str(payload.get("reason_for_choice") or ""),
        required_capabilities=[str(item) for item in required_capabilities],
    )


def _approval_status(value: Any) -> str:
    text = str(value or "pending")
    return text if text in {"pending", "approved", "skipped"} else "pending"


def _strip_json_fence(raw_output: str) -> str:
    text = raw_output.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


__all__ = ["DerivedConsideration", "LlmOutputParser"]
