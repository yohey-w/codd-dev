"""Validation helpers for LLM-selected verification strategies."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from codd.deployment.providers.llm_consideration import Consideration as DerivedConsideration


LOGGER = logging.getLogger(__name__)


class StrategyValidator:
    """Filter considerations that reference unavailable verification engines."""

    def validate(
        self,
        considerations: list[DerivedConsideration],
        registry: Mapping[str, Any],
    ) -> list[DerivedConsideration]:
        kept: list[DerivedConsideration] = []
        for consideration in considerations:
            strategy = consideration.verification_strategy
            engine = getattr(strategy, "engine", None)
            if engine is not None and engine != "" and engine not in registry:
                LOGGER.warning(
                    "Skipping LLM consideration %s: verification engine is not registered: %s",
                    consideration.id,
                    engine,
                )
                continue
            kept.append(consideration)
        return kept


__all__ = ["StrategyValidator"]
