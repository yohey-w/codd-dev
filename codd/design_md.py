"""DESIGN.md parser for W3C-style design tokens."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

import yaml


REFERENCE_RE = re.compile(r"\{([^}]+)\}")

_FRONT_MATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)\Z", re.DOTALL)
_SECTION_CATEGORIES = {
    "colors": "color",
    "typography": "typography",
    "rounded": "spacing",
    "spacing": "spacing",
    "components": "component",
}
_METADATA_KEYS = {"version", "name", "description"}


@dataclass
class DesignToken:
    id: str
    category: str
    value: Any
    references: list[str] = field(default_factory=list)


@dataclass
class DesignMdResult:
    tokens: list[DesignToken] = field(default_factory=list)
    body_md: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class DesignMdExtractor:
    """Extract design tokens from a DESIGN.md YAML front matter block."""

    def extract(self, path: Path) -> DesignMdResult:
        path = Path(path)
        if not path.exists():
            return DesignMdResult(error=f"not found: {path}")

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return DesignMdResult(error=f"read error: {exc}")

        match = _FRONT_MATTER_RE.match(content)
        if not match:
            return DesignMdResult(body_md=content)

        front_matter, body_md = match.groups()
        try:
            raw_data = yaml.safe_load(front_matter) or {}
        except yaml.YAMLError as exc:
            return DesignMdResult(error=f"parse error: {exc}")

        if not isinstance(raw_data, dict):
            return DesignMdResult(error="parse error: front matter must contain a YAML mapping")

        metadata = {key: raw_data[key] for key in _METADATA_KEYS if key in raw_data}
        tokens: list[DesignToken] = []

        for section, value in raw_data.items():
            if section in _METADATA_KEYS:
                continue
            category = _SECTION_CATEGORIES.get(section, "other")
            tokens.extend(_tokens_from_section(section, category, value))

        return DesignMdResult(tokens=tokens, body_md=body_md, metadata=metadata)


def _tokens_from_section(prefix: str, category: str, value: Any) -> list[DesignToken]:
    if not isinstance(value, dict):
        return [
            DesignToken(
                id=prefix,
                category=category,
                value=value,
                references=_extract_references(value),
            )
        ]

    tokens: list[DesignToken] = []
    for name, item_value in value.items():
        token_id = f"{prefix}.{name}"
        if _should_descend(item_value):
            tokens.extend(_tokens_from_section(token_id, category, item_value))
            continue
        tokens.append(
            DesignToken(
                id=token_id,
                category=category,
                value=item_value,
                references=_extract_references(item_value),
            )
        )
    return tokens


def _should_descend(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    return all(isinstance(child, dict) for child in value.values())


def _extract_references(value: Any) -> list[str]:
    refs: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, str):
            for match in REFERENCE_RE.findall(node):
                ref = match.strip()
                if ref and ref not in refs:
                    refs.append(ref)
            return
        if isinstance(node, dict):
            for child in node.values():
                visit(child)
            return
        if isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return refs
