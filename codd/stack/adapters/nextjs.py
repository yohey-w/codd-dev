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


# ── route_handler_must_be_exercised (WARN) ───────────────────────────────────

_ROUTE_EXTS = (".ts", ".tsx", ".js", ".jsx")
_TEST_MARKERS = (".spec.", ".test.")
# Test trees that count as exercising a route (e2e/integration/smoke; unit tests
# rarely drive HTTP routes). Searched under the project root.
_TEST_DIRS = ("tests", "test", "e2e", "__tests__", "cypress")


def _route_handlers(root: Path) -> list[Path]:
    """Discover Next.js route handlers: App Router ``app/**/route.{ts,js,…}`` and
    Pages Router ``pages/api/**``. Searches the project root and a ``src/`` base
    (the curated profile's two layout conventions)."""
    handlers: list[Path] = []
    for base in (root, root / "src"):
        app = base / "app"
        if app.is_dir():
            handlers += [p for p in app.rglob("route.*") if p.suffix in _ROUTE_EXTS]
        api = base / "pages" / "api"
        if api.is_dir():
            handlers += [
                p
                for p in api.rglob("*")
                if p.is_file()
                and p.suffix in _ROUTE_EXTS
                and not any(m in p.name for m in _TEST_MARKERS)
            ]
    return handlers


def _route_signals(handler: Path, root: Path) -> tuple[str, ...]:
    """Best-effort URL signals a test would reference for this handler.

    App Router ``…/app/api/health/route.ts`` → ``/api/health`` (route groups
    ``(grp)`` are dropped from the URL); Pages ``…/pages/api/foo/bar.ts`` →
    ``/api/foo/bar``. Also returns the last static path segment as a looser signal.
    Dynamic ``[id]`` segments are kept as a prefix only (a test uses a concrete id),
    so matching is intentionally lenient — this is a WARN advisory, biased toward
    "covered" to avoid false-RED."""
    parts = list(handler.parts)
    anchor = None
    for key in ("app", "pages"):
        if key in parts:
            anchor = parts.index(key)
            break
    if anchor is None:
        return ()
    segs = parts[anchor + 1 :]
    if segs and segs[-1].startswith("route."):
        segs = segs[:-1]  # App Router: drop the route.* filename
    elif segs:
        segs[-1] = Path(segs[-1]).stem  # Pages: drop the extension
    if parts[anchor] == "pages" and segs and segs[0] != "api":
        return ()  # only API routes are "handlers" we require exercising
    url_segs = [s for s in segs if not (s.startswith("(") and s.endswith(")"))]
    if not url_segs:
        return ()
    # Static prefix up to the first dynamic [..] segment (a test references a
    # concrete value past that point, so we don't require the literal [id]).
    static_prefix: list[str] = []
    for s in url_segs:
        if s.startswith("[") :
            break
        static_prefix.append(s)
    signals = set()
    if static_prefix:
        signals.add("/" + "/".join(static_prefix))
        signals.add(static_prefix[-1])  # looser: the last static segment name
    return tuple(sorted(signals))


def check_route_coverage(project_root: str | Path, **_: object) -> list[ObligationFinding]:
    """``route_handler_must_be_exercised`` (WARN): every discovered route handler
    should be referenced by >=1 e2e/integration test.

    Best-effort + conservative (WARN, biased toward "covered"): a handler is
    considered exercised if a route signal (its URL path, or its last static
    segment) appears in any e2e/integration/smoke test file's text. Handlers with
    NO such reference are flagged. This is a heuristic — an indirectly-exercised
    handler can be missed — so it is WARN, never a hard gate. Returns [] when there
    are no handlers or no test tree (nothing to assert)."""
    root = Path(project_root)
    handlers = _route_handlers(root)
    if not handlers:
        return []
    test_text_parts: list[str] = []
    for tdir in _TEST_DIRS:
        d = root / tdir
        if not d.is_dir():
            continue
        for tf in d.rglob("*"):
            if (
                tf.is_file()
                and tf.suffix in _ROUTE_EXTS
                and any(m in tf.name for m in _TEST_MARKERS)
            ):
                test_text_parts.append(tf.read_text(encoding="utf-8", errors="replace"))
    if not test_text_parts:
        return []  # no test tree to evidence against — nothing to assert (WARN)
    test_text = "\n".join(test_text_parts)
    findings: list[ObligationFinding] = []
    for handler in handlers:
        signals = _route_signals(handler, root)
        if signals and any(sig in test_text for sig in signals):
            continue
        try:
            loc = str(handler.relative_to(root))
        except ValueError:
            loc = handler.name
        label = signals[0] if signals else handler.name
        findings.append(
            ObligationFinding(
                obligation_id="route_handler_must_be_exercised",
                location=loc,
                detail=f"route handler {label} is not referenced by any e2e/integration test",
            )
        )
    return findings
