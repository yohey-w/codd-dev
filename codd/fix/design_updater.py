"""Update a design_doc in response to a PHENOMENON analysis."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from codd.fix.phenomenon_parser import PhenomenonAnalysis
from codd.fix.templates_loader import load_template, render_template
from codd.frontmatter import parse_frontmatter

AiInvoke = Callable[[str], str]

_FRONTMATTER_CODD_BLOCK_RE = re.compile(
    r"^codd:\s*$.*?(?=^\S|\Z)", re.MULTILINE | re.DOTALL
)

_FRONTMATTER_DELIMITER_RE = re.compile(r"^\ufeff?---[ \t]*\r?$", re.MULTILINE)
_MARKDOWN_PREFACE_RE = re.compile(
    r"^(?:[#>+*~_<|\[`-]|\d+[.)](?:[ \t]|$))|```|~~~"
)
#: One line of prose ahead of the frontmatter is never document content (the
#: document starts at its frontmatter), so the cap only bounds what is dropped.
#: Observed prefaces from real runs were 13, 67 and 191 characters long.
_MAX_PREFACE_CHARS = 400


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
    original = target_path.read_text(encoding="utf-8")

    template = load_template("design_update.txt", override=template_path)
    prompt = render_template(
        template,
        phenomenon_text=phenomenon_text.strip(),
        analysis_json=json.dumps(analysis.to_dict(), ensure_ascii=False),
        allow_delete="true" if allow_delete else "false",
        target_path=str(target_path),
        current_content=original,
    )

    if retry_feedback is not None:
        prompt += "\n" + retry_feedback + "\n"

    raw = ai_invoke(prompt)
    proposed = _strip_code_fence(raw)
    if not proposed.strip():
        raise DesignUpdateError("LLM returned empty document body")

    # Only recover a form that the legacy metadata check would reject.
    original_block = _extract_codd_block(original)
    reason = ""
    if original_block and _extract_codd_block(proposed) != original_block:
        document = _strip_short_preface(raw)
        if document is not None:
            proposed = document
            reason = "Discarded a short preface before the frontmatter."

    _assert_codd_metadata_preserved(original_block, proposed)
    if not allow_delete:
        _assert_no_required_section_removed(original, proposed)

    diff = _make_diff(target_path, original, proposed)
    changed = original != proposed

    return DesignUpdate(
        target_path=target_path,
        original_content=original,
        proposed_content=proposed,
        diff=diff,
        changed=changed,
        reason=reason,
    )


def apply_update(update: DesignUpdate) -> None:
    """Write the proposed content to disk (no-op when unchanged)."""
    if update.is_no_op():
        return
    update.target_path.write_text(update.proposed_content, encoding="utf-8")


def _strip_code_fence(raw: str) -> str:
    text = raw.strip()
    fenced = re.match(r"^```(?:[\w-]+)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip() + "\n"
    return text + ("\n" if not text.endswith("\n") else "")


def _strip_short_preface(raw: str) -> str | None:
    """Recognize one line of plain prose before a complete YAML frontmatter."""
    opening = _FRONTMATTER_DELIMITER_RE.search(raw)
    if opening is None or opening.start() == 0:
        return None

    line, _, blank_lines = raw[:opening.start()].partition("\n")
    if "\n" not in blank_lines or blank_lines.strip():
        return None

    line = line.removesuffix("\r").lstrip("\ufeff")
    prose = line.lstrip()
    if (
        not prose
        or len(line) > _MAX_PREFACE_CHARS
        or line.splitlines() != [line]
        or line.startswith("    ")
        or "\t" in line
        or _MARKDOWN_PREFACE_RE.search(prose)
    ):
        return None

    document = _strip_code_fence(raw[opening.start():])
    try:
        frontmatter = parse_frontmatter(document)
        if frontmatter.error is not None or "codd" not in frontmatter.mapping:
            return None
        following = parse_frontmatter(frontmatter.body.lstrip())
    except (RecursionError, ValueError):
        # Keep malformed model output on the normal validation/retry path.
        return None
    if following.has_block and following.error is None:
        return None
    return document


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


def _assert_codd_metadata_preserved(original_block: str, proposed: str) -> None:
    proposed_block = _extract_codd_block(proposed)
    if original_block == proposed_block:
        return

    opening = _FRONTMATTER_DELIMITER_RE.search(proposed)
    if original_block and (
        opening is None or not _FRONTMATTER_DELIMITER_RE.search(proposed, opening.end())
    ):
        reason = "frontmatter is missing or incomplete"
    elif original_block and opening is not None and opening.start():
        reason = "the document must start with its original frontmatter"
    elif not proposed_block:
        reason = "frontmatter 'codd:' block is missing (tooling-owned)"
    else:
        reason = "frontmatter 'codd:' block was modified (tooling-owned)"
    raise DesignUpdateError(
        f"design_updater: {reason}. Return only the full updated document, "
        "preserve the original frontmatter verbatim, and omit any preface."
    )


def _extract_codd_block(text: str) -> str:
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if not fm_match:
        return ""
    fm = fm_match.group(1)
    block_match = _FRONTMATTER_CODD_BLOCK_RE.search(fm)
    if not block_match:
        return ""
    return block_match.group(0).rstrip()


def _make_diff(target_path: Path, original: str, proposed: str) -> str:
    diff_lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=f"a/{target_path}",
        tofile=f"b/{target_path}",
        n=3,
    )
    return "".join(diff_lines)
