"""Reusable cooperative HITL session state for CoDD commands."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
UTC = timezone.utc
import os
from pathlib import Path
import sys
from typing import Any

import yaml

from codd.coherence_engine import DriftEvent, EventBus
from codd.lexicon import (
    AskItem,
    LEXICON_FILENAME,
    ProjectLexicon,
    ask_item_from_dict,
    ask_item_to_dict,
    validate_lexicon,
)


_coherence_bus: EventBus | None = None


class HitlSession:
    """Manage ASK decisions that can proceed with recommendations before answers."""

    def __init__(self, ask_items: list[AskItem] | None = None):
        self.ask_items: list[AskItem] = list(ask_items or [])

    def add_ask(self, item: AskItem) -> None:
        if not item.asked_at:
            item = replace(item, asked_at=_utc_now_iso())
        self.ask_items.append(item)

    def proceed_with_recommended(self) -> dict[str, str]:
        """Move all non-blocking ASK items to RECOMMENDED_PROCEEDING."""
        proceeded: dict[str, str] = {}
        for item in self.ask_items:
            if item.status != "ASK" or item.blocking:
                continue
            recommended_id = _recommended_id(item)
            if not recommended_id:
                continue
            item.recommended_id = recommended_id
            item.proceeded_with = recommended_id
            item.status = "RECOMMENDED_PROCEEDING"
            proceeded[item.id] = recommended_id
        return proceeded

    def apply_answer(self, item_id: str, answer: str) -> bool:
        """Apply a human answer and return True when after-fact patching is needed."""
        item = self._find(item_id)
        item.answer = answer
        item.answered_at = _utc_now_iso()
        recommended_id = item.recommended_id or item.proceeded_with or _recommended_id(item)
        if answer == recommended_id:
            item.status = "CONFIRMED"
            return False
        item.status = "OVERRIDDEN"
        _publish_requirement_override_drift(item, answer, recommended_id)
        return True

    def save_to_lexicon(self, lexicon_path: Path) -> None:
        """Persist current ASK state to project_lexicon.yaml coverage_decisions."""
        data = _load_lexicon_data(lexicon_path)
        lexicon = ProjectLexicon(data)
        lexicon.set_coverage_decisions(self.ask_items)
        output = lexicon.as_dict()
        validate_lexicon(output)
        lexicon_path.parent.mkdir(parents=True, exist_ok=True)
        lexicon_path.write_text(
            yaml.safe_dump(output, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def load_from_lexicon(self, lexicon_path: Path) -> None:
        """Load ASK state from project_lexicon.yaml coverage_decisions."""
        data = _load_lexicon_data(lexicon_path)
        self.ask_items = [
            ask_item_from_dict(item)
            for item in data.get("coverage_decisions", [])
        ]

    def _find(self, item_id: str) -> AskItem:
        for item in self.ask_items:
            if item.id == item_id:
                return item
        raise KeyError(f"ASK item not found: {item_id}")


def set_coherence_bus(bus: EventBus | None) -> None:
    """Install the coherence bus used for requirement override drift events."""
    global _coherence_bus
    _coherence_bus = bus


def is_claude_code_env() -> bool:
    """Return True when Claude Code specific AskUserQuestion transport may exist."""
    truthy = {"1", "true", "yes", "on"}
    if os.environ.get("CLAUDE_CODE_ENV", "").lower() in truthy:
        return True
    if os.environ.get("CLAUDECODE", "").lower() in truthy:
        return True
    return bool(os.environ.get("CLAUDE_CODE_SESSION") and sys.stdin.isatty())


def _load_lexicon_data(lexicon_path: Path) -> dict[str, Any]:
    if not lexicon_path.exists():
        return {
            "node_vocabulary": [],
            "naming_conventions": [],
            "design_principles": [],
            "coverage_decisions": [],
        }
    data = yaml.safe_load(lexicon_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{LEXICON_FILENAME} must contain a YAML mapping")
    data.setdefault("node_vocabulary", [])
    data.setdefault("naming_conventions", [])
    data.setdefault("design_principles", [])
    data.setdefault("coverage_decisions", [])
    return data


def _recommended_id(item: AskItem) -> str | None:
    if item.recommended_id:
        return item.recommended_id
    for option in item.options:
        if option.recommended:
            return option.id
    if item.options:
        return item.options[0].id
    return None


def _publish_requirement_override_drift(
    item: AskItem,
    answer: str,
    recommended_id: str | None,
) -> None:
    if _coherence_bus is None:
        return
    _coherence_bus.publish(
        DriftEvent(
            source_artifact="requirements",
            target_artifact="design_doc",
            change_type="modified",
            payload={
                "source": "requirement_decision",
                "target": "design_documents",
                "ask_id": item.id,
                "question": item.question,
                "recommended_id": recommended_id,
                "answer": answer,
                "description": "Human requirement answer differs from the proceeded recommendation.",
                "suggested_action": "Patch derived design documents with the overridden requirement decision.",
            },
            severity="amber",
            fix_strategy="hitl",
            kind="requirement_override_drift",
        )
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
