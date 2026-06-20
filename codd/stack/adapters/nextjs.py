"""Next.js obligation checkers — the ENFORCEMENT behind the Next.js profile's
declared obligations (so they are not declarative theater).

The headline checker is the design's anti-false-green star (§1): a Next.js
``next.config.*`` may set ``typescript.ignoreBuildErrors: true`` (and
``eslint.ignoreDuringBuilds: true``), which lets ``next build`` COMPLETE WITH
type/lint errors. When either is set, ``next build`` MUST NOT be accepted as
typecheck/lint evidence — that would be a false green. This checker detects the
unsafe settings so the obligation can red.

The detector is comment-stripped (a commented-out setting must NOT flag — that
would be a false RED) and matches the literal ``: true`` form (``: false`` and
absent are clean). next.config is JS/TS code; a dynamically-computed value can't
be resolved statically, so this is a best-effort LITERAL detector — but the
default posture is already safe (the curated profile keeps ``typecheck`` as the
language's ``tsc --noEmit``, separate from ``framework_build``), so this checker
only fires when a project actually writes the dangerous literal.
"""

from __future__ import annotations

import re
from pathlib import Path

from ._base import ObligationFinding

_CONFIG_NAMES = (
    "next.config.js",
    "next.config.mjs",
    "next.config.cjs",
    "next.config.ts",
)

# Comment-stripped matches for the two "ignore checks during build" settings.
_IGNORE_TS_ERRORS = re.compile(r"ignoreBuildErrors\s*:\s*true\b")
_IGNORE_ESLINT = re.compile(r"ignoreDuringBuilds\s*:\s*true\b")


def _strip_js_comments(src: str) -> str:
    """Remove /* block */ and // line comments so a commented-out setting does
    not false-flag (anti-false-RED). String-literal edge cases are tolerated:
    the settings of interest are booleans, never inside strings."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def check_ignore_build_errors(project_root: str | Path, **_: object) -> list[ObligationFinding]:
    """Return findings if next.config.* disables type/lint checking during build.

    Non-empty result == the ``no_ignore_build_errors_as_typecheck`` obligation is
    VIOLATED (``next build`` is not valid typecheck/lint evidence).
    """
    root = Path(project_root)
    findings: list[ObligationFinding] = []
    for name in _CONFIG_NAMES:
        cfg = root / name
        if not cfg.exists():
            continue
        src = _strip_js_comments(cfg.read_text(encoding="utf-8", errors="replace"))
        if _IGNORE_TS_ERRORS.search(src):
            findings.append(
                ObligationFinding(
                    obligation_id="no_ignore_build_errors_as_typecheck",
                    location=name,
                    detail="typescript.ignoreBuildErrors: true — next build completes WITH type errors",
                )
            )
        if _IGNORE_ESLINT.search(src):
            findings.append(
                ObligationFinding(
                    obligation_id="no_ignore_build_errors_as_typecheck",
                    location=name,
                    detail="eslint.ignoreDuringBuilds: true — next build completes WITH lint errors",
                )
            )
    return findings
