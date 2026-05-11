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

AiInvoke = Callable[[str], str]

_FRONTMATTER_CODD_BLOCK_RE = re.compile(
    r"^codd:\s*$.*?(?=^\S|\Z)", re.MULTILINE | re.DOTALL
)


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

    raw = ai_invoke(prompt)
    proposed = _strip_code_fence(raw)
    if not proposed.strip():
        raise DesignUpdateError("LLM returned empty document body")

    if not allow_delete:
        _assert_no_required_section_removed(original, proposed)

    _assert_codd_metadata_preserved(original, proposed)

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
    update.target_path.write_text(update.proposed_content, encoding="utf-8")


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


def _assert_codd_metadata_preserved(original: str, proposed: str) -> None:
    original_block = _extract_codd_block(original)
    proposed_block = _extract_codd_block(proposed)
    if original_block != proposed_block:
        raise DesignUpdateError(
            "design_updater: frontmatter 'codd:' block was modified (tooling-owned)"
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
