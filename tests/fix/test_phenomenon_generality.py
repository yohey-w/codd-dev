"""Generality validation tests: codd.fix must not encode project-specific terms.

Per the LMS-overfit-protection gate (cmd_466 / cmd_467), the codd core
source for codd.fix must NEVER contain references to particular projects
the tooling was demoed on (osato-lms, stripe, prisma-specific concepts
that aren't generic DB migration words, etc.).
"""

from __future__ import annotations

import re
from pathlib import Path


FORBIDDEN_TOKENS = [
    "osato",
    "osato-lms",
    "lms-demo",
    "shogun",
    "yohey",
    "yohei",
    "sym-",
    "tono",
    "kekkon",
    "ohaka",
    "zeirishi",
    "paters",
    "kagi-erabi",
]


def _codd_fix_files() -> list[Path]:
    root = Path(__file__).resolve().parents[2] / "codd" / "fix"
    return [p for p in root.rglob("*.py")] + list(root.rglob("*.txt"))


def test_no_project_specific_tokens_in_codd_fix_source():
    pattern = re.compile("|".join(re.escape(t) for t in FORBIDDEN_TOKENS), re.IGNORECASE)
    offenders: list[tuple[str, str]] = []
    for path in _codd_fix_files():
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            if re.search(re.escape(token), text, re.IGNORECASE):
                offenders.append((str(path), token))
    assert not offenders, f"project-specific tokens leaked into codd.fix: {offenders}"


def test_phenomenon_templates_use_placeholders_not_hardcoded_questions():
    """Templates should use {placeholder} fields, not hardcoded user-facing
    sentences pretending to be questions/options.

    The acceptance criterion: clarification options must come from the LLM,
    not from hardcoded literals embedded in templates.
    """
    templates_dir = Path(__file__).resolve().parents[2] / "codd" / "fix" / "templates"
    clarification_text = (templates_dir / "clarification_question.txt").read_text(encoding="utf-8")

    # Must instruct the LLM to derive options from analysis / lexicon.
    assert "lexicon" in clarification_text.lower()
    assert "{phenomenon_text}" in clarification_text
    assert "{analysis_json}" in clarification_text


def test_no_hardcoded_japanese_in_interactive_prompt():
    """The InteractivePrompt class itself must stay language-agnostic.

    Question strings are passed in by the caller; the prompt class should
    only contain neutral I/O glue (numbers, 'abort', 'yes/no').
    """
    path = Path(__file__).resolve().parents[2] / "codd" / "fix" / "interactive_prompt.py"
    text = path.read_text(encoding="utf-8")
    # Japanese-only contains hiragana/katakana/CJK ideographs.
    cjk_pattern = re.compile(r"[぀-ヿ㐀-鿿]")
    assert not cjk_pattern.search(text), (
        "InteractivePrompt should not contain Japanese — questions must come from caller"
    )
