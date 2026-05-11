"""Detect risky operations in a proposed diff."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from codd.fix.templates_loader import load_template, render_template

AiInvoke = Callable[[str], str]


@dataclass
class RiskAssessment:
    """Risk classification of a unified diff."""

    risky: bool = False
    categories: list[str] = field(default_factory=list)
    summary: str = ""
    heuristic_flags: list[str] = field(default_factory=list)


SCHEMA_MIGRATION_PATTERNS = (
    re.compile(r"\bprisma/migrations\b"),
    re.compile(r"\balembic/versions\b"),
    re.compile(r"^[+\-].*?\b(CREATE|ALTER|DROP)\s+TABLE\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\.sql\b"),
)

DEPENDENCY_FILE_PATTERNS = (
    re.compile(r"^\+\+\+ b/(?:.*/)?pyproject\.toml", re.MULTILINE),
    re.compile(r"^\+\+\+ b/(?:.*/)?package\.json", re.MULTILINE),
    re.compile(r"^\+\+\+ b/(?:.*/)?requirements[\w\-.]*\.txt", re.MULTILINE),
    re.compile(r"^\+\+\+ b/(?:.*/)?Gemfile", re.MULTILINE),
    re.compile(r"^\+\+\+ b/(?:.*/)?Cargo\.toml", re.MULTILINE),
)

TEST_FILE_PATTERNS = (
    re.compile(r"^---\s+a/(?:.*/)?test_[^/]+\.(?:py|js|ts|tsx)$", re.MULTILINE),
    re.compile(r"^---\s+a/(?:.*/)?[^/]+\.spec\.(?:py|js|ts|tsx)$", re.MULTILINE),
    re.compile(r"^---\s+a/(?:.*/)?[^/]+\.test\.(?:py|js|ts|tsx)$", re.MULTILINE),
)

CONFIG_FILE_PATTERNS = (
    re.compile(r"^\+\+\+ b/\.github/workflows/", re.MULTILINE),
    re.compile(r"^\+\+\+ b/(?:.*/)?Dockerfile", re.MULTILINE),
    re.compile(r"^\+\+\+ b/(?:.*/)?\.env(\..+)?$", re.MULTILINE),
)


def classify_risk(
    diff_text: str,
    *,
    ai_invoke: AiInvoke | None = None,
    template_path=None,
) -> RiskAssessment:
    """Combine heuristic detection with an optional LLM judgment.

    When ai_invoke is None (e.g. unit tests, --no-llm-risk), the
    heuristic verdict is used as-is.
    """
    if not diff_text or not diff_text.strip():
        return RiskAssessment()

    heuristic_flags = _detect_heuristics(diff_text)

    if ai_invoke is None:
        return RiskAssessment(
            risky=bool(heuristic_flags),
            categories=list(heuristic_flags),
            summary=", ".join(heuristic_flags) if heuristic_flags else "",
            heuristic_flags=list(heuristic_flags),
        )

    template = load_template("risk_assessment.txt", override=template_path)
    prompt = render_template(
        template,
        diff_text=diff_text[:8000],
        heuristic_flags=json.dumps(heuristic_flags),
    )

    try:
        raw = ai_invoke(prompt)
    except Exception:  # noqa: BLE001
        return RiskAssessment(
            risky=bool(heuristic_flags),
            categories=list(heuristic_flags),
            summary="LLM unavailable; using heuristic verdict",
            heuristic_flags=list(heuristic_flags),
        )

    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        return RiskAssessment(
            risky=bool(heuristic_flags),
            categories=list(heuristic_flags),
            summary="LLM output unparseable; using heuristic verdict",
            heuristic_flags=list(heuristic_flags),
        )

    risky_field = bool(parsed.get("risky"))
    cats_field = parsed.get("categories")
    categories: list[str] = []
    if isinstance(cats_field, list):
        for item in cats_field:
            if isinstance(item, str) and item.strip():
                categories.append(item.strip())
    summary = str(parsed.get("summary", "") or "").strip()

    # Union heuristic flags with LLM categories so we never under-report.
    merged_categories = list(dict.fromkeys(heuristic_flags + categories))
    risky = risky_field or bool(heuristic_flags)

    return RiskAssessment(
        risky=risky,
        categories=merged_categories,
        summary=summary,
        heuristic_flags=list(heuristic_flags),
    )


def _detect_heuristics(diff_text: str) -> list[str]:
    flags: list[str] = []

    if any(pattern.search(diff_text) for pattern in SCHEMA_MIGRATION_PATTERNS):
        flags.append("schema_migration")
    if any(pattern.search(diff_text) for pattern in DEPENDENCY_FILE_PATTERNS):
        flags.append("dependency_add")
    if any(pattern.search(diff_text) for pattern in TEST_FILE_PATTERNS):
        if _has_test_removal(diff_text):
            flags.append("test_removal")
    if any(pattern.search(diff_text) for pattern in CONFIG_FILE_PATTERNS):
        flags.append("config_change")
    if _is_mass_deletion(diff_text):
        flags.append("mass_deletion")

    return flags


def _is_mass_deletion(diff_text: str) -> bool:
    added = 0
    removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    if removed < 20:
        return False
    return removed >= 3 * max(1, added)


def _has_test_removal(diff_text: str) -> bool:
    """Heuristic: a test file appears in `---` (removed-side) hunks and the
    diff contains deletions of `def test_` / `it(...)` / `test(...)` lines."""
    if not any(pattern.search(diff_text) for pattern in TEST_FILE_PATTERNS):
        return False
    if re.search(r"^-\s*def\s+test_", diff_text, re.MULTILINE):
        return True
    if re.search(r"^-\s*(?:it|test)\(", diff_text, re.MULTILINE):
        return True
    return False


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json(raw: str):
    if not raw:
        return None
    text = raw.strip()
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
