"""Suggest lexicon plug-ins from generic stack detection results."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import re

import yaml

from codd.lexicon import LEXICON_FILENAME


@dataclass(frozen=True)
class StackMapEntry:
    hint_pattern: str
    suggested_lexicons: tuple[str, ...]


def default_stack_map_path() -> Path:
    return Path(__file__).resolve().parents[2] / "codd_plugins" / "stack_map.yaml"


def default_lexicon_root() -> Path:
    return Path(__file__).resolve().parents[2] / "codd_plugins" / "lexicons"


def load_stack_map(path: Path | None = None) -> list[StackMapEntry]:
    source = path or default_stack_map_path()
    payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    rows = payload.get("stack_map", [])
    if not isinstance(rows, list):
        raise ValueError("stack_map.yaml must contain a stack_map list")
    entries: list[StackMapEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("stack_map entries must be mappings")
        pattern = str(row.get("hint_pattern", "")).strip()
        lexicons = tuple(str(item).strip() for item in row.get("suggested_lexicons", []) if str(item).strip())
        if not pattern or not lexicons:
            raise ValueError("stack_map entries require hint_pattern and suggested_lexicons")
        re.compile(pattern)
        entries.append(StackMapEntry(pattern, lexicons))
    return entries


def suggest_lexicons(stack_hints: Sequence[str], entries: Sequence[StackMapEntry]) -> list[str]:
    suggested: list[str] = []
    seen: set[str] = set()
    for hint in stack_hints:
        for entry in entries:
            if not re.search(entry.hint_pattern, hint, flags=re.IGNORECASE):
                continue
            for lexicon in entry.suggested_lexicons:
                if lexicon in seen:
                    continue
                seen.add(lexicon)
                suggested.append(lexicon)
    return suggested


def describe_lexicons(lexicon_ids: Iterable[str], lexicon_root: Path | None = None) -> dict[str, str]:
    root = lexicon_root or default_lexicon_root()
    descriptions: dict[str, str] = {}
    for lexicon_id in lexicon_ids:
        manifest = root / lexicon_id / "manifest.yaml"
        if not manifest.is_file():
            descriptions[lexicon_id] = ""
            continue
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        descriptions[lexicon_id] = str(payload.get("description", ""))
    return descriptions


def append_suggested_lexicons(project_root: Path, lexicon_ids: Sequence[str]) -> Path:
    path = Path(project_root) / LEXICON_FILENAME
    data = _load_project_lexicon(path)
    current = data.get("suggested_lexicons", [])
    if not isinstance(current, list):
        raise ValueError("project_lexicon.yaml suggested_lexicons must be a list")
    known = {_lexicon_id(item) for item in current}
    merged = list(current)
    for lexicon_id in lexicon_ids:
        if lexicon_id in known:
            continue
        merged.append(lexicon_id)
        known.add(lexicon_id)
    data["suggested_lexicons"] = merged
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def _load_project_lexicon(path: Path) -> dict[str, Any]:
    if path.is_file():
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise ValueError("project_lexicon.yaml must contain a YAML mapping")
        return payload
    return {
        "version": "1.0",
        "node_vocabulary": [],
        "naming_conventions": [],
        "design_principles": [
            "Review suggested lexicons before treating project conventions as approved.",
        ],
    }


def _lexicon_id(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("id", item.get("name", item.get("lexicon_name", ""))))
    return str(item)

