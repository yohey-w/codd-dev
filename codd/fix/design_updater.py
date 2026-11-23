"""Update a design_doc in response to a PHENOMENON analysis."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from codd.fix.phenomenon_parser import PhenomenonAnalysis
from codd.fix.templates_loader import load_template, render_template
from codd.frontmatter import parse_frontmatter

AiInvoke = Callable[[str], str]

_FRONTMATTER_RE = re.compile(
    r"\A\ufeff?[ \t]*---[ \t]*(?P<newline>\r\n|\r|\n)(?P<header>.*?)"
    r"(?<![^\r\n])[ \t]*---[ \t]*(?:\r\n|\r|\n|\Z)",
    re.DOTALL,
)
_FRONTMATTER_CODD_BLOCK_RE = re.compile(
    r"^codd:\s*$.*?(?=^\S|\Z)", re.MULTILINE | re.DOTALL
)
_CODD_METADATA_ERROR = "design_updater: frontmatter 'codd:' block was modified (tooling-owned)"


class DesignUpdateError(Exception):
    """The proposed update violates the preservation contract."""


@dataclass
class DesignUpdate:
    """Result of design_updater.update()."""

    target_path: Path
    original_content: str
    proposed_content: str
    diff: str
    changed: bool
    reason: str = ""

    def is_no_op(self) -> bool:
        return not self.changed


def update_design_doc(
    target_path: Path,
    *,
    phenomenon_text: str,
    analysis: PhenomenonAnalysis,
    ai_invoke: AiInvoke,
    allow_delete: bool = False,
    template_path: Path | None = None,
    retry_feedback: str | None = None,
) -> DesignUpdate:
    """Ask the LLM to produce an updated body for a target design_doc.

    Enforces:
    - frontmatter `codd:` block is preserved byte-for-byte.
    - No deletion of existing user_journeys/acceptance_criteria unless
      allow_delete is True.
    """
    target_path = Path(target_path)
    with target_path.open(encoding="utf-8", newline="") as source:
        original = source.read()
        newline = source.newlines
    normalized_original = _normalize_newlines(original)

    template = load_template("design_update.txt", override=template_path)
    prompt = render_template(
        template,
        phenomenon_text=phenomenon_text.strip(),
        analysis_json=json.dumps(analysis.to_dict(), ensure_ascii=False),
        allow_delete="true" if allow_delete else "false",
        target_path=str(target_path),
        current_content=normalized_original,
    )
    if retry_feedback is not None:
        prompt += "\n" + retry_feedback + "\n"

    raw = ai_invoke(prompt)
    proposed = _strip_code_fence(raw)
    if not proposed.strip():
        raise DesignUpdateError("LLM returned empty document body")

    if not allow_delete:
        _assert_no_required_section_removed(normalized_original, proposed)

    if newline in ("\r", "\r\n"):
        proposed = _normalize_newlines(proposed).replace("\n", newline)
    proposed = _preserve_codd_metadata(original, proposed)

    diff = _make_diff(target_path, original, proposed)
    changed = original != proposed

    return DesignUpdate(
        target_path=target_path,
        original_content=original,
        proposed_content=proposed,
        diff=diff,
        changed=changed,
    )


def apply_update(update: DesignUpdate) -> None:
    """Write the proposed content to disk (no-op when unchanged)."""
    if update.is_no_op():
        return
    update.target_path.write_text(update.proposed_content, encoding="utf-8", newline="")


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    fenced = re.match(r"^```(?:[\w-]+)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip() + "\n"
    return text + ("\n" if not text.endswith("\n") else "")


REQUIRED_SECTION_KEYS = (
    "user_journeys",
    "acceptance_criteria",
    "personas",
    "lexicon_terms",
)


def _assert_no_required_section_removed(original: str, proposed: str) -> None:
    """Reject diff if a required section is removed without permission."""
    for key in REQUIRED_SECTION_KEYS:
        original_count = _count_section_entries(original, key)
        proposed_count = _count_section_entries(proposed, key)
        if proposed_count < original_count:
            raise DesignUpdateError(
                f"design_updater: '{key}' entries reduced "
                f"({original_count} → {proposed_count}); use --allow-delete to override"
            )


def _count_section_entries(text: str, key: str) -> int:
    """Approximate: count YAML list items under a top-level key.

    Walks forward from the key line until a less-indented sibling (or
    end-of-frontmatter `---`) is encountered, counting any line whose
    first non-whitespace token is `- ` (a list-item marker).
    """
    key_pattern = re.compile(rf"^{re.escape(key)}:\s*$", re.MULTILINE)
    match = key_pattern.search(text)
    if not match:
        return 0

    count = 0
    for line in text[match.end():].splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            # Next top-level key or end-of-frontmatter `---` reached.
            break
        if line.lstrip().startswith("- "):
            count += 1
    return count


def _preserve_codd_metadata(original: str, proposed: str) -> str:
    original_fm = _FRONTMATTER_RE.match(original)
    proposed_fm = _FRONTMATTER_RE.match(proposed)
    original_span = _codd_block_span(original_fm)
    proposed_span = _codd_block_span(proposed_fm)
    missing = object()
    original_value = parse_frontmatter(original).mapping.get("codd", missing)
    parsed = parse_frontmatter(proposed)
    proposed_value = parsed.mapping.get("codd", missing)

    if original_span is None:
        if proposed_span is not None or proposed_value != original_value:
            raise DesignUpdateError(_CODD_METADATA_ERROR)
        return proposed

    original_start, original_end = original_span
    saved_block = original[original_start:original_end]
    if proposed_span is not None:
        start, end = proposed_span
        if (
            proposed_value != original_value
            or _normalize_newlines(proposed[start:end]).rstrip()
            != _normalize_newlines(saved_block).rstrip()
        ):
            raise DesignUpdateError(_CODD_METADATA_ERROR)
        restored = proposed[:start] + saved_block + proposed[end:]
    else:
        if proposed_value is not missing or original_value is missing:
            raise DesignUpdateError(_CODD_METADATA_ERROR)
        assert original_fm is not None
        if proposed_fm is None:
            if parsed.error:
                raise DesignUpdateError(_CODD_METADATA_ERROR)
            opening = original[:original_fm.start("header")]
            closing = original[original_fm.end("header"):original_fm.end()]
            if not closing.endswith(("\r", "\n")):
                closing += original_fm.group("newline")
            restored = opening + saved_block + closing + proposed
        else:
            insertion = proposed_fm.end("header")
            original_prefix = original[original_fm.start("header"):original_start]
            if proposed_fm.group("header").startswith(original_prefix):
                insertion = proposed_fm.start("header") + len(original_prefix)
            restored = proposed[:insertion] + saved_block + proposed[insertion:]
            if parsed.error:
                # A restored anchor can make previously unparseable YAML
                # readable. Check for duplicate keys before comparing values.
                _codd_block_span(_FRONTMATTER_RE.match(restored))

    # An alias or surrounding YAML can affect the meaning of the restored text.
    if parse_frontmatter(restored).mapping.get("codd", missing) != original_value:
        raise DesignUpdateError(_CODD_METADATA_ERROR)
    return restored


def _codd_block_span(frontmatter: re.Match[str] | None) -> tuple[int, int] | None:
    """Locate the original text of a block-mapping entry without dumping YAML."""
    if frontmatter is None:
        return None
    offset = frontmatter.start("header")
    header = frontmatter.group("header")
    try:
        root = yaml.compose(header, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        # Keep the existing text check for legacy documents with malformed YAML.
        legacy = _FRONTMATTER_CODD_BLOCK_RE.search(header)
        return (offset + legacy.start(), offset + legacy.end()) if legacy else None
    if not isinstance(root, yaml.MappingNode):
        return None
    keys = [key for key, _value in root.value]
    indexes = [
        index for index, key in enumerate(keys)
        if isinstance(key, yaml.ScalarNode) and key.value == "codd"
    ]
    if len(indexes) > 1:
        raise DesignUpdateError(_CODD_METADATA_ERROR)
    if not indexes or root.flow_style:
        return None
    index = indexes[0]
    start = keys[index].start_mark
    lines = header.splitlines(keepends=True)
    end_line = keys[index + 1].start_mark.line if index + 1 < len(keys) else len(lines)
    # A comment between the key and value is part of the slice. Dedented
    # comments following the value belong to the surrounding frontmatter.
    for line_number in range(end_line - 1, start.line, -1):
        line = lines[line_number]
        stripped = line.lstrip()
        if not stripped:
            continue
        if not stripped.startswith("#") or len(line) - len(stripped) > start.column:
            break
        end_line = line_number
    return (
        offset + start.index - start.column,
        offset + sum(len(line) for line in lines[:end_line]),
    )


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _make_diff(target_path: Path, original: str, proposed: str) -> str:
    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=f"a/{target_path}",
        tofile=f"b/{target_path}",
        n=3,
    )
    return "".join(diff_lines)
