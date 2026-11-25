"""Number Markdown requirement items and render their source for derivation."""

from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Any

import yaml


_QUOTE = re.compile(r"^(?:[ \t]*>[ \t]?)+")
_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_SETEXT = re.compile(r"(?:=+|-+)")
_THEMATIC_BREAK = re.compile(r"(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,}")
_LIST_ITEM = re.compile(r"^(?:[-+*]|[0-9]{1,9}[.)])(?: {1,4}(?! )| |$)")
_FENCE = re.compile(r"^(?:`{3,}(?!`)[^`]*|~{3,}.*)$")
_TABLE_PIPE = re.compile(r"(?<!\\)(?:\\\\)*\|")
_TABLE_DIVIDER = re.compile(r":?-+:?")


def read_requirement_snapshot(path: Path) -> tuple[str, str]:
    """Read the prompt text and fingerprint the same source bytes."""
    content = path.read_bytes()
    text = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return text, sha256(content).hexdigest()


def number_requirements(documents: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], str]:
    """Return the source spans and complete prompt text with reference markers."""
    items: list[dict[str, Any]] = []
    sources: list[str] = []
    for path, text in documents:
        lines = text.split("\n")
        markers = {}
        for start, end in _requirement_ranges(lines):
            item_id = f"R{len(items) + 1}"
            markers[start] = item_id
            items.append({
                "id": item_id,
                "path": path,
                "start_line": start + 1,
                "end_line": end,
                "text": "\n".join(lines[start:end]),
            })
        sources.append(f"Document: {path}")
        sources.extend(f"{markers.get(i, ''):>8} | {line}" for i, line in enumerate(lines))
        sources.append("")
    return items, "\n".join(sources)


@dataclass(frozen=True)
class _Line:
    text: str
    indent: int
    quote_depth: int


def _markdown_line(raw: str) -> _Line:
    raw = raw.lstrip("\ufeff")
    quote = _QUOTE.match(raw)
    prefix = quote.group() if quote else ""
    visible = raw[len(prefix):].expandtabs(4)
    text = visible.lstrip()
    return _Line(text.rstrip(), len(visible) - len(text), prefix.count(">"))


def _table_cells(line: str) -> list[str]:
    cells = _TABLE_PIPE.split(line.removeprefix("|"))
    return cells[:-1] if len(cells) > 1 and not cells[-1] else cells


def _starts_table(lines: list[_Line], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header, rule = lines[index:index + 2]
    if header.quote_depth != rule.quote_depth or not (
        _TABLE_PIPE.search(header.text) or _TABLE_PIPE.search(rule.text)
    ):
        return False
    divider = _table_cells(rule.text)
    return len(divider) == len(_table_cells(header.text)) and all(
        _TABLE_DIVIDER.fullmatch(cell.strip()) for cell in divider
    )


def _starts_block(text: str) -> bool:
    return bool(_HEADING.match(text) or _THEMATIC_BREAK.fullmatch(text)
                or _LIST_ITEM.match(text) or _FENCE.match(text))


def _fence_end(lines: list[_Line], index: int, opening: str, depth: int, indent: int) -> int:
    marker = opening[0]
    length = len(opening) - len(opening.lstrip(marker))
    while index < len(lines):
        line = lines[index]
        if line.quote_depth < depth or (line.quote_depth == depth and line.text and line.indent < indent):
            break
        index += 1
        if (line.quote_depth == depth and line.indent < indent + 4
                and len(line.text) >= length and not line.text.strip(marker)):
            break
    return index


def _list_end(lines: list[_Line], start: int, marker_end: int) -> int:
    first = lines[start]
    indent = first.indent + marker_end
    index = start + 1
    if _FENCE.match(first.text[marker_end:]):
        index = _fence_end(lines, index, first.text[marker_end:], first.quote_depth, indent)
    while index < len(lines):
        line = lines[index]
        if line.quote_depth != first.quote_depth:
            break
        if not line.text:
            continuation = index + 1
            while (continuation < len(lines) and not lines[continuation].text
                   and lines[continuation].quote_depth == first.quote_depth):
                continuation += 1
            if (continuation == len(lines) or lines[continuation].indent < indent
                    or lines[continuation].quote_depth != first.quote_depth):
                break
            line = lines[continuation]
        else:
            continuation = index

        if indent <= line.indent < indent + 4 and _FENCE.match(line.text):
            index = _fence_end(lines, continuation + 1, line.text, line.quote_depth, indent)
        elif _starts_block(line.text) or _starts_table(lines, continuation) or line.indent < first.indent:
            break
        else:
            index = continuation + 1
    return index


def _front_matter_end(lines: list[_Line]) -> int:
    """Return the line after a leading YAML front matter mapping, or 0.

    Document metadata is context, not a requirement. Only a fenced block at the
    very start that parses as a YAML mapping qualifies, so a document that merely
    opens with a thematic break keeps every line it contains.
    """
    if not lines or lines[0].text != "---" or lines[0].indent or lines[0].quote_depth:
        return 0
    for end in range(1, len(lines)):
        closing = lines[end]
        if closing.text in {"---", "..."} and not closing.indent and not closing.quote_depth:
            block = "\n".join(" " * line.indent + line.text for line in lines[1:end])
            try:
                metadata = yaml.safe_load(block)
            except yaml.YAMLError:
                return 0
            return end + 1 if isinstance(metadata, dict) else 0
    return 0


def _requirement_ranges(source: list[str]) -> Iterator[tuple[int, int]]:
    """Yield half-open spans for rows, list items, paragraphs, and fenced blocks.

    Front matter, headings, table headers and separators stay in the prompt as
    context. Other nonblank text falls back to paragraphs, without interpreting
    its wording.
    """
    lines = [_markdown_line(line) for line in source]
    index = _front_matter_end(lines)
    while index < len(lines):
        line = lines[index]
        if not line.text:
            index += 1
            continue
        start = index
        marker = _LIST_ITEM.match(line.text)
        if _THEMATIC_BREAK.fullmatch(line.text) and line.indent < 4:
            index += 1
            continue
        if marker:
            index = _list_end(lines, index, marker.end())
        elif line.indent >= 4:
            index += 1
            while (index < len(lines) and lines[index].quote_depth == line.quote_depth
                   and (not lines[index].text or lines[index].indent >= 4)):
                index += 1
        elif _HEADING.match(line.text):
            index += 1
            continue
        elif _starts_table(lines, index):
            index += 2
            while index < len(lines):
                row = lines[index]
                if (row.quote_depth != line.quote_depth or row.indent >= 4 or not row.text
                        or _starts_block(row.text) or _starts_table(lines, index)):
                    break
                yield index, index + 1
                index += 1
            continue
        elif _FENCE.match(line.text):
            index = _fence_end(lines, index + 1, line.text, line.quote_depth, 0)
        else:
            index += 1
            while index < len(lines):
                next_line = lines[index]
                if next_line.quote_depth != line.quote_depth or not next_line.text:
                    break
                if next_line.indent < 4 and _SETEXT.fullmatch(next_line.text):
                    # A setext heading can span multiple consecutive lines.
                    start = index = index + 1
                    break
                if _starts_block(next_line.text) or _starts_table(lines, index):
                    break
                index += 1
        if start < index:
            yield start, index
