"""Single source of truth for YAML frontmatter parsing.

YAML frontmatter is the authoritative carrier of CoDD metadata
(``codd.node_id``, ``depends_on``, conventions, restoration provenance).
Historically seven near-duplicate parsers diverged on nested-``codd:``
handling, error behavior, body splitting, and list coercion. Every reader
of ``---``-delimited frontmatter must go through this module.

Semantics
---------
* A frontmatter block is an opening line whose stripped content is ``---``
  (a UTF-8 BOM before it is tolerated) followed by a closing line whose
  stripped content is ``---``. The YAML between the delimiters must load
  to a mapping (an empty block loads to ``{}``).
* Lenient mode (default) never raises: absence, an unclosed block, or
  invalid YAML yield an empty mapping; ``strict=True`` raises
  :class:`FrontmatterError` with a machine-readable ``code``.
* The body returned by :func:`split_frontmatter` preserves the original
  text bytes after the closing delimiter line (no re-joining of lines).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

__all__ = [
    "FrontmatterError",
    "ParsedFrontmatter",
    "apply_aliases",
    "as_list",
    "codd_block",
    "frontmatter_or_yaml_payload",
    "parse_frontmatter",
    "read_frontmatter",
    "split_frontmatter",
]

_BOM = "\ufeff"
_DELIMITER = "---"

#: Error codes carried by :class:`FrontmatterError` / :class:`ParsedFrontmatter`.
ERROR_UNCLOSED = "unclosed"
ERROR_INVALID_YAML = "invalid_yaml"
ERROR_NOT_MAPPING = "not_mapping"
ERROR_READ = "read_error"


class FrontmatterError(ValueError):
    """Raised in strict mode when a frontmatter block is malformed."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedFrontmatter:
    """Rich parse result for call sites that need exact legacy semantics.

    ``body`` is the text after the closing delimiter line when a complete
    block was found (even if its YAML was invalid or not a mapping); when no
    complete block exists, ``body`` is the original text.
    """

    mapping: dict[str, Any]
    body: str
    has_block: bool
    error: str | None = None
    error_message: str | None = None
    exception: BaseException | None = None


def parse_frontmatter(text: str) -> ParsedFrontmatter:
    """Parse a ``---``-delimited YAML frontmatter block. Never raises."""
    stripped = text.lstrip(_BOM)
    lines = stripped.splitlines(keepends=True)
    if not lines or lines[0].strip() != _DELIMITER:
        return ParsedFrontmatter(mapping={}, body=text, has_block=False)

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() != _DELIMITER:
            continue
        raw = "".join(lines[1:index])
        body = "".join(lines[index + 1 :])
        try:
            loaded = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            return ParsedFrontmatter(
                mapping={},
                body=body,
                has_block=True,
                error=ERROR_INVALID_YAML,
                error_message=f"invalid YAML frontmatter: {exc}",
                exception=exc,
            )
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            return ParsedFrontmatter(
                mapping={},
                body=body,
                has_block=True,
                error=ERROR_NOT_MAPPING,
                error_message="frontmatter must be a YAML mapping",
            )
        return ParsedFrontmatter(mapping=loaded, body=body, has_block=True)

    return ParsedFrontmatter(
        mapping={},
        body=text,
        has_block=False,
        error=ERROR_UNCLOSED,
        error_message="frontmatter is missing a closing delimiter",
    )


def split_frontmatter(text: str, *, strict: bool = False) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(frontmatter_mapping, body)``.

    Lenient (default): absence, an unclosed block, or invalid YAML return
    ``({}, original_text)``; a complete block whose YAML is valid but not a
    mapping returns ``({}, body_after_block)``. Strict: any malformed block
    raises :class:`FrontmatterError` (absence still returns ``({}, text)``).
    """
    result = parse_frontmatter(text)
    if result.error is not None:
        if strict:
            raise FrontmatterError(
                result.error_message or "malformed frontmatter", code=result.error
            ) from result.exception
        if result.error == ERROR_NOT_MAPPING:
            return {}, result.body
        return {}, text
    if not result.has_block:
        return {}, text
    return result.mapping, result.body


def read_frontmatter(path: Path | str, *, strict: bool = False) -> dict[str, Any] | None:
    """Read ``path`` and return its frontmatter mapping.

    Lenient (default): returns ``None`` when the file is unreadable, carries
    no complete frontmatter block, or the block is malformed. Strict: raises
    :class:`FrontmatterError` on read failure or a malformed block (absence
    of a block still returns ``None``).
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        if strict:
            raise FrontmatterError(f"failed to read file: {exc}", code=ERROR_READ) from exc
        return None

    result = parse_frontmatter(text)
    if result.error is not None:
        if strict:
            raise FrontmatterError(
                result.error_message or "malformed frontmatter", code=result.error
            ) from result.exception
        return None
    if not result.has_block:
        return None
    return result.mapping


def codd_block(
    frontmatter: Mapping[str, Any] | None, *, fallback_top_level: bool = False
) -> dict[str, Any] | None:
    """Return the nested ``codd:`` mapping from parsed frontmatter.

    CoDD-generated documents nest their metadata under a top-level ``codd:``
    key; legacy/flat documents put the same keys at the top level. With
    ``fallback_top_level=True`` the whole frontmatter mapping is returned
    when no nested ``codd:`` mapping exists.
    """
    if not isinstance(frontmatter, Mapping):
        return None
    nested = frontmatter.get("codd")
    if isinstance(nested, dict):
        return nested
    if isinstance(nested, Mapping):
        return dict(nested)
    if fallback_top_level:
        return frontmatter if isinstance(frontmatter, dict) else dict(frontmatter)
    return None


def frontmatter_or_yaml_payload(path: Path | str) -> dict[str, Any] | None:
    """Return a document's YAML payload: frontmatter for ``.md``, else whole file.

    Markdown files must carry a complete, valid frontmatter mapping; any
    other file is parsed as a whole-file YAML mapping (empty file → ``{}``).
    Returns ``None`` when the file is unreadable or the payload is not a
    mapping. Never raises.
    """
    path = Path(path)
    try:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None

    if path.suffix == ".md":
        result = parse_frontmatter(text)
        if not result.has_block or result.error is not None:
            return None
        return dict(result.mapping)

    try:
        payload = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def as_list(value: Any) -> list[Any]:
    """Coerce a frontmatter value to a list: ``None`` → ``[]``, scalar → ``[scalar]``."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def apply_aliases(
    frontmatter: dict[str, Any],
    alias_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return frontmatter with configured alias keys copied to canonical keys.

    ``alias_map`` maps ``alias_key -> canonical_key`` (the
    ``extraction.frontmatter_alias`` mechanism). The canonical key always
    wins when both are present; alias keys are kept alongside the copy. The
    input mapping is never mutated.
    """
    if not isinstance(alias_map, Mapping) or not alias_map:
        return deepcopy(frontmatter)

    resolved = deepcopy(frontmatter)
    for alias_key, canonical_key in _normalized_alias_map(alias_map).items():
        if alias_key in resolved and canonical_key not in resolved:
            resolved[canonical_key] = deepcopy(resolved[alias_key])
    return resolved


def _normalized_alias_map(alias_map: Mapping[str, str]) -> dict[str, str]:
    return {
        str(alias_key).strip(): str(canonical_key).strip()
        for alias_key, canonical_key in alias_map.items()
        if str(alias_key).strip() and str(canonical_key).strip()
    }
