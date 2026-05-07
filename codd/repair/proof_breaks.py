"""Helpers for deterministic proof-break fixtures in repair smoke tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

import yaml


PROOF_BREAK_PLACEHOLDER = "<!-- CODD_PROOF_BREAK_PLACEHOLDER -->"


class ProofBreakNotFound(ValueError):
    """Raised when a requested proof-break anchor cannot be found."""


def replace_yaml_list_item_by_value(
    content: str,
    *,
    list_key: str,
    match_key: str,
    match_value: str,
    replacement_item: Mapping[str, Any],
) -> str:
    """Replace a YAML frontmatter list item by matching one field value.

    The helper is intentionally structural rather than exact-text based: it
    preserves the Markdown body and unrelated frontmatter while allowing the
    target list item to gain, lose, or reorder sibling fields over time.
    """

    return _replace_yaml_list_item_by_value(
        content,
        list_key=list_key,
        match_key=match_key,
        match_value=match_value,
        replacement_factory=lambda indent: _render_yaml_list_item(replacement_item, indent),
    )


def replace_yaml_list_item_block_by_value(
    content: str,
    *,
    list_key: str,
    match_key: str,
    match_value: str,
    replacement_block: str,
) -> str:
    """Replace a YAML frontmatter list item with a pre-rendered block."""

    return _replace_yaml_list_item_by_value(
        content,
        list_key=list_key,
        match_key=match_key,
        match_value=match_value,
        replacement_factory=lambda indent: _render_yaml_list_item_block(replacement_block, indent),
    )


def _replace_yaml_list_item_by_value(
    content: str,
    *,
    list_key: str,
    match_key: str,
    match_value: str,
    replacement_factory: Callable[[int], list[str]],
) -> str:
    lines = content.splitlines(keepends=True)
    bounds = _frontmatter_bounds(lines)
    if bounds is None:
        raise ProofBreakNotFound("YAML frontmatter not found")

    frontmatter_start, frontmatter_end = bounds
    section = _find_yaml_list_section(lines, frontmatter_start, frontmatter_end, list_key)
    if section is None:
        raise ProofBreakNotFound(f"{list_key} list not found in YAML frontmatter")

    _key_line, item_start, item_end, key_indent = section
    item_ranges = _yaml_list_item_ranges(lines, item_start, item_end, key_indent)
    for start, end, item_indent in item_ranges:
        item = _load_list_item(lines[start:end])
        if item is None or str(item.get(match_key)) != str(match_value):
            continue
        replacement = replacement_factory(item_indent)
        return "".join([*lines[:start], *replacement, *lines[end:]])

    raise ProofBreakNotFound(f"{list_key} item with {match_key}={match_value} not found")


def ensure_proof_break_placeholder(
    content: str,
    placeholder: str = PROOF_BREAK_PLACEHOLDER,
) -> str:
    """Return Markdown content with a stable proof-break placeholder comment."""

    if placeholder in content:
        return content

    lines = content.splitlines(keepends=True)
    bounds = _frontmatter_bounds(lines)
    insertion = 0 if bounds is None else bounds[1] + 1
    insert_lines = [f"{placeholder}\n", "\n"]
    return "".join([*lines[:insertion], *insert_lines, *lines[insertion:]])


def _frontmatter_bounds(lines: list[str]) -> tuple[int, int] | None:
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return 1, index
    return None


def _find_yaml_list_section(
    lines: list[str],
    frontmatter_start: int,
    frontmatter_end: int,
    list_key: str,
) -> tuple[int, int, int, int] | None:
    for index in range(frontmatter_start, frontmatter_end):
        line = lines[index]
        if _is_blank_or_comment(line):
            continue
        stripped = line.strip()
        if not stripped.startswith(f"{list_key}:"):
            continue
        if stripped.split(":", 1)[0].strip() != list_key:
            continue
        key_indent = _indent_width(line)
        start = index + 1
        end = start
        while end < frontmatter_end:
            candidate = lines[end]
            if not _is_blank_or_comment(candidate) and _indent_width(candidate) <= key_indent:
                break
            end += 1
        return index, start, end, key_indent
    return None


def _yaml_list_item_ranges(
    lines: list[str],
    section_start: int,
    section_end: int,
    key_indent: int,
) -> list[tuple[int, int, int]]:
    starts: list[tuple[int, int]] = []
    item_indent: int | None = None
    for index in range(section_start, section_end):
        line = lines[index]
        indent = _indent_width(line)
        if indent <= key_indent or not line[indent:].startswith("-"):
            continue
        if item_indent is None:
            item_indent = indent
        if indent == item_indent:
            starts.append((index, indent))

    ranges: list[tuple[int, int, int]] = []
    for offset, (start, indent) in enumerate(starts):
        end = starts[offset + 1][0] if offset + 1 < len(starts) else section_end
        ranges.append((start, end, indent))
    return ranges


def _load_list_item(lines: list[str]) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load("".join(lines))
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, list) or not loaded:
        return None
    item = loaded[0]
    return dict(item) if isinstance(item, Mapping) else None


def _render_yaml_list_item(item: Mapping[str, Any], indent: int) -> list[str]:
    rendered = yaml.safe_dump(
        [dict(item)],
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=1_000_000,
    )
    if not rendered.endswith("\n"):
        rendered += "\n"
    prefix = " " * indent
    return [f"{prefix}{line}" if line.strip() else line for line in rendered.splitlines(keepends=True)]


def _render_yaml_list_item_block(block: str, indent: int) -> list[str]:
    text = block.strip("\n")
    if not text:
        raise ProofBreakNotFound("replacement block must not be empty")
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] = f"{lines[-1]}\n"
    source_indent = min((_indent_width(line) for line in lines if line.strip()), default=0)
    prefix = " " * indent
    return [f"{prefix}{line[source_indent:]}" if line.strip() else line for line in lines]


def _is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


__all__ = [
    "PROOF_BREAK_PLACEHOLDER",
    "ProofBreakNotFound",
    "ensure_proof_break_placeholder",
    "replace_yaml_list_item_block_by_value",
    "replace_yaml_list_item_by_value",
]
