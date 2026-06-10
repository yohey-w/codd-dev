"""Shared AI patch mechanism: prompt → fenced file blocks → guarded writes.

Extracted from ``codd.fixer`` so the legacy failure-driven ``codd fix`` and
the PHENOMENON-mode implementation propagation share one battle-tested block
parser and write path. The LLM boundary is deliberately narrow: the model
only produces *text*; this module deterministically parses fenced code
blocks tagged with file paths and writes only permitted paths inside the
project root. Verification and rollback live with the callers.

``run_fix`` behavior is preserved exactly: the regex patterns, their
priority order, the path-dedup semantics, the outside-project guard, and
the log messages are byte-for-byte moves from ``codd.fixer``.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection

from codd.claude_cli import with_default_claude_permission_bypass

logger = logging.getLogger("codd.ai_patch")

# Regex patterns to extract fenced code blocks tagged with file paths.

# Primary: ```language path/to/file.py
FIX_BLOCK_RE = re.compile(
    r"```[a-zA-Z]*\s+([\w./_-]+)\s*\n(.*?)```",
    re.DOTALL,
)

# Fallback 1: **path/to/file.py** or `path/to/file.py` on preceding line
# followed by a code block
FIX_BLOCK_PRECEDED_RE = re.compile(
    r"(?:\*\*|`)([\w./_-]+\.\w+)(?:\*\*|`)\s*:?\s*\n```[a-zA-Z]*\s*\n(.*?)```",
    re.DOTALL,
)

# Fallback 2: // filepath: path/to/file.py as first line inside code block
FIX_BLOCK_COMMENT_RE = re.compile(
    r"```[a-zA-Z]*\s*\n\s*(?://|#)\s*(?:filepath|file):\s*([\w./_-]+)\s*\n(.*?)```",
    re.DOTALL,
)

# System prompt optimized for code fix (not document generation)
DEFAULT_FIX_SYSTEM_PROMPT = (
    "You are a code repair assistant. You receive error logs, current source code, "
    "and design documents. Output the complete fixed source for each file in fenced "
    "code blocks tagged with the file path. Do not output explanations before the "
    "code blocks. Fix implementation to match the design specification."
)


@dataclass
class PatchApplication:
    """Outcome of applying AI-produced file blocks to disk."""

    ai_output: str = ""
    applied_paths: list[str] = field(default_factory=list)
    created_paths: list[str] = field(default_factory=list)  # subset of applied
    skipped_paths: list[str] = field(default_factory=list)


def parse_fix_blocks(ai_output: str) -> list[tuple[str, str]]:
    """Parse fenced code blocks tagged with file paths from AI output.

    Tries all patterns, primary first; a path matched by a higher-priority
    pattern is never overridden by a lower-priority one (same dedup
    semantics as the historical ``codd.fixer._invoke_fix_ai``).
    """
    blocks: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for pattern in (FIX_BLOCK_RE, FIX_BLOCK_PRECEDED_RE, FIX_BLOCK_COMMENT_RE):
        for match in pattern.finditer(ai_output):
            file_path_str = match.group(1)
            if file_path_str in seen_paths:
                continue  # Already captured by a higher-priority pattern
            seen_paths.add(file_path_str)
            blocks.append((file_path_str, match.group(2)))
    return blocks


def apply_fix_blocks(
    blocks: list[tuple[str, str]],
    project_root: Path,
    *,
    allowed_paths: Collection[str] | None = None,
    allow_path: Callable[[str], bool] | None = None,
) -> PatchApplication:
    """Write parsed file blocks to disk, honoring an optional write allowlist.

    A block is written when its path resolves inside ``project_root`` AND
    (no allowlist was given OR the path is in ``allowed_paths`` OR
    ``allow_path(path)`` is true). Everything else is skipped with a
    warning — the LLM never gets implicit write access beyond the contract.
    """
    application = PatchApplication()
    allowed = set(allowed_paths) if allowed_paths is not None else None
    restrict = allowed is not None or allow_path is not None

    for file_path_str, fixed_code in blocks:
        target = project_root / file_path_str
        if not target.resolve().is_relative_to(project_root.resolve()):
            logger.warning("Skipping file outside project: %s", file_path_str)
            application.skipped_paths.append(file_path_str)
            continue

        if restrict:
            permitted = bool(allowed and file_path_str in allowed) or bool(
                allow_path and allow_path(file_path_str)
            )
            if not permitted:
                logger.warning(
                    "Skipping file outside the permitted write set: %s", file_path_str
                )
                application.skipped_paths.append(file_path_str)
                continue

        if not target.exists():
            application.created_paths.append(file_path_str)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(fixed_code, encoding="utf-8")
        application.applied_paths.append(file_path_str)
        logger.info("Applied fix to: %s", file_path_str)

    if not application.applied_paths:
        logger.warning("AI output contained no parseable file blocks to apply")

    return application


def prepare_fix_ai_command(
    ai_command: str,
    *,
    system_prompt: str = DEFAULT_FIX_SYSTEM_PROMPT,
) -> str:
    """Adapt an AI command for fix mode.

    If the command contains a --system-prompt intended for document generation,
    replace it with the fix-optimized system prompt.
    """
    parts = with_default_claude_permission_bypass(shlex.split(ai_command))
    cleaned: list[str] = []
    skip_next = False
    has_system_prompt = False

    for tok in parts:
        if skip_next:
            skip_next = False
            continue
        if tok == "--system-prompt":
            skip_next = True
            has_system_prompt = True
            continue
        cleaned.append(tok)

    # Add fix-specific system prompt
    if has_system_prompt or "--print" in cleaned:
        cleaned.extend(["--system-prompt", system_prompt])

    return shlex.join(cleaned)


def invoke_fix_ai(
    ai_command: str,
    prompt: str,
    project_root: Path,
    *,
    system_prompt: str = DEFAULT_FIX_SYSTEM_PROMPT,
    allowed_paths: Collection[str] | None = None,
    allow_path: Callable[[str], bool] | None = None,
) -> PatchApplication:
    """Invoke AI in --print mode and apply returned code blocks to files.

    The prompt includes the original source and error log.  The AI returns
    the **complete fixed source** for each file, wrapped in fenced code
    blocks tagged with file paths::

        ```typescript src/app/api/enrollments/route.ts
        // ... fixed code ...
        ```

    This function parses those blocks and writes them back to disk.
    Uses multiple regex patterns for robustness (primary + 2 fallbacks).
    With no allowlist arguments this is behavior-identical to the historical
    ``codd.fixer._invoke_fix_ai``.
    """
    from codd.generator import _invoke_ai_command  # lazy: keep import light

    fixed_command = prepare_fix_ai_command(ai_command, system_prompt=system_prompt)
    ai_output = _invoke_ai_command(fixed_command, prompt)

    blocks = parse_fix_blocks(ai_output)
    application = apply_fix_blocks(
        blocks,
        project_root,
        allowed_paths=allowed_paths,
        allow_path=allow_path,
    )
    application.ai_output = ai_output
    return application
