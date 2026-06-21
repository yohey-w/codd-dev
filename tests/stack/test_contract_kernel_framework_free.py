"""Static gate (Contract Kernel Cut Condition B §B.1): the generation CORE stays
framework-literal-free — FOREVER.

The framework half of the language-free gate (``tests/languages/
test_contract_kernel_language_free.py``). Cut Condition B = framework-pluggable
stack: the harness core branches on NO framework NAME. All framework knowledge
lives in the declarative stack profiles (``codd/stack/profiles/**``), the stack
adapters (``codd/stack/adapters/**``) and the stack checkers — never as an
``if framework == "<name>"`` dispatch in the generation core. A new framework
joins via profile/adapter/checker, with no core edit (goal doc §B.1, lines 65-68).

This test FAILS the moment a framework-name literal DISPATCH (``framework ==
"Playwright"``, ``framework in ("next", ...)``, ``self.framework == "cypress"``,
``template_ref == "playwright"`` used as a branch, …) is reintroduced into a
LOCKED core module, so the Cut Condition B achievement for the generation zone
cannot silently regress.

LOCKED zones (asserted framework-literal-free here):
  * codd/generator.py — the generation prompt builders. The historical
    ``if framework == "Playwright" and is_browser_e2e:`` Playwright-codegen-rules
    DISPATCH was the ONE framework-name branch here. It was a REDUNDANT framework
    conjunct: ``framework`` was a LOCAL variable derived purely from the output
    file extension (``"Playwright" if ext in (".ts", ".js") else "pytest"``), and
    ``is_browser_e2e`` ALREADY required ``ext in (".ts", ".js")`` — so
    ``framework == "Playwright" and is_browser_e2e`` was byte-identical to
    ``is_browser_e2e`` alone. The de-literalization collapsed the conjunct to the
    harness/extension gate (``is_browser_e2e``) — the guidance was never truly
    dispatched by a framework NAME, only by the browser-e2e harness decision (the
    deterministic :func:`codd.e2e_harness.resolve_e2e_harness` already governs the
    ``.ts``-vs-``.py`` harness/extension choice). No framework-name branch remains.

What is FORBIDDEN here is a framework-name DISPATCH (a ``==`` / ``!=`` / ``in``
comparison whose other operand is a quoted framework name). What is ALLOWED — and
deliberately NOT flagged — is a framework name used as:
  * CONTENT / guidance prose fed to the generating model (string literals such as
    ``"Do NOT use Playwright/Cypress"`` / ``"the historical TS Playwright
    guidance"``). The generation prompt legitimately *talks about* frameworks; it
    just must not *branch on* a framework name. (Matching is CODE-ONLY — docstrings
    + comments are stripped — and the dispatch idiom requires a comparison
    operator, which a bare string literal does not have.)
  * a display-string ASSIGNMENT (``framework = "Playwright"`` — a single ``=``,
    used to render the prompt header "You are generating executable Playwright
    test code"). An assignment that builds a display string is not a dispatch
    branch; the goal doc forbids the framework-name *分岐* (BRANCH), not naming a
    framework in content. (This mirrors the language gate, which forbids
    ``language == / in / .resolve("<lang>")`` dispatch but never the per-language
    display names / registry-data strings.)

KNOWN separately-tracked zone (NOT locked here — documented so its exclusion is
honest, mirroring the language gate's former ``PENDING_ZONES``):
  * codd/e2e_generator.py — the LEGACY "E2E stub from extracted scenarios"
    renderer (``TestGenerator``), invoked by its own CLI command
    (``codd/cli.py`` ``TestGenerator(project_root, base_url=..., framework=...)``)
    with an EXPLICIT user-selected ``framework`` parameter
    (``SUPPORTED_FRAMEWORKS = {"playwright", "cypress"}``). It is a DIFFERENT
    subsystem from the Contract-Kernel generation pipeline (``generator.py`` does
    not import it) and still carries ``self.framework == "cypress"`` dispatch.
    De-literalizing that user-parameterized stub renderer is out of scope for the
    generation-core gate; it is recorded in :data:`KNOWN_UNLOCKED_FRAMEWORK_ZONES`
    so this exclusion is explicit and a future increment can graduate it.
"""

from __future__ import annotations

import re
from pathlib import Path

import codd

# ── the locked core modules (relative to the package root) ───────────────────
LOCKED_MODULES = (
    # The generation prompt builders. The ONE framework-name DISPATCH here (the
    # ``framework == "Playwright"`` Playwright-codegen-rules branch) was collapsed
    # to the byte-identical harness/extension gate ``is_browser_e2e`` — no
    # framework-name branch remains. See module docstring for the equivalence.
    "generator.py",
)

# Framework names whose literal DISPATCH is forbidden in the locked core (the set
# from goal doc §B.1: next/nextjs/prisma/playwright/react/django/rails/express/
# fastapi, plus cypress — the e2e addon paired with playwright).
_FRAMEWORK_NAMES = (
    "next",
    "nextjs",
    "prisma",
    "playwright",
    "react",
    "django",
    "rails",
    "express",
    "fastapi",
    "cypress",
)
_FRAMEWORK_ALT = "|".join(re.escape(n) for n in _FRAMEWORK_NAMES)

# Framework-name literal DISPATCH idioms forbidden in the core. Matched against
# CODE ONLY (docstrings + comments stripped), so prose that *describes* or *names*
# a framework (guidance strings, this repo's architecture docs) is not matched —
# only a live comparison BRANCH against a framework-name literal is. The dispatch
# idiom REQUIRES a comparison operator (``==`` / ``!=`` / ``in``); a bare string
# literal (guidance prose) or a single-``=`` assignment (display string) has none.
_FORBIDDEN = (
    # ``framework == "<x>"`` / ``framework != "<x>"`` — the variable-named dispatch
    # (case-insensitive value so ``framework == "Playwright"`` is caught). The
    # negative lookbehind on ``=`` keeps a single ``=`` assignment from matching.
    (
        re.compile(r"""\bframework\b\s*(?:==|!=)\s*['"]"""),
        'framework == "<framework>" literal compare (dispatch)',
    ),
    # ``framework in (...)`` / ``framework not in [...]`` — set-membership dispatch.
    (
        re.compile(r"""\bframework\b\s+(?:not\s+)?in\s*[\(\[{]"""),
        "framework in (...) literal set dispatch",
    ),
    # ``<anything> == "<framework-name>"`` — a framework-name dispatch whose LHS is
    # not literally spelled ``framework`` (e.g. ``self.framework == "cypress"``,
    # ``template_ref == "playwright"`` used as a branch). Targets the SPECIFIC
    # framework names, so prose like ``"Playwright/Cypress"`` (no comparison
    # operator) and an ``e2e_harness: playwright`` profile key are not matched.
    (
        re.compile(rf"""(?<![\w.])(?:==|!=)\s*['"](?:{_FRAMEWORK_ALT})['"]""", re.IGNORECASE),
        '== "<framework>" literal compare (e.g. self.framework == "cypress")',
    ),
    # ``load_addon_profile("playwright")`` / ``registry.resolve("playwright")`` — a
    # framework-name dispatch HIDDEN behind a profile/registry load. This is the
    # main false-green trap (per the GPT-5.5 consult): "moving guidance to YAML but
    # still selecting it by ``load_*_profile("<fw>")`` / ``.resolve("<fw>")`` is the
    # SAME dispatch behind a different literal." A profile is selected by the resolved
    # STACK CONTRACT, never by a hardcoded framework name in the locked core.
    (
        re.compile(
            rf"""(?ix)
            (?:\b(?:load|resolve|select|get)_[a-z0-9_]*(?:profile|addon|template|harness)[a-z0-9_]*
               |\.(?:resolve|load|get))
            \(\s*['"](?:{_FRAMEWORK_ALT})['"]
            """,
        ),
        'load/resolve "<framework>" by name (hidden profile/registry dispatch)',
    ),
)

# KNOWN separately-tracked, NOT-yet-locked framework zones. Documented so the
# exclusion is HONEST (mirrors the language gate's former ``PENDING_ZONES``): the
# legacy ``TestGenerator`` stub renderer is a user-``framework``-parameterized
# subsystem distinct from the generation core; graduating it is a future increment.
KNOWN_UNLOCKED_FRAMEWORK_ZONES = ("e2e_generator.py",)

_PKG_ROOT = Path(codd.__file__).resolve().parent

# Strip triple-quoted docstrings/blocks and # line-comments so explanatory PROSE
# (which legitimately mentions framework names) is not matched — only live CODE is.
_TRIPLE = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)
_LINE_COMMENT = re.compile(r"#.*?$", re.MULTILINE)


def _code_only(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    src = _TRIPLE.sub("\n", src)
    src = _LINE_COMMENT.sub("", src)
    return src


def _scan(code: str) -> list[tuple[int, str, str]]:
    """Return ``(line, label, snippet)`` for every forbidden dispatch in *code*."""
    hits: list[tuple[int, str, str]] = []
    for pattern, label in _FORBIDDEN:
        for m in pattern.finditer(code):
            line = code.count("\n", 0, m.start()) + 1
            snippet = code[m.start() : m.start() + 60].splitlines()[0].strip()
            hits.append((line, label, snippet))
    return hits


def test_generation_core_is_framework_free() -> None:
    """Every LOCKED core module is free of framework-name literal dispatch."""
    violations: list[str] = []
    for rel in LOCKED_MODULES:
        path = _PKG_ROOT / rel
        assert path.is_file(), f"locked module missing: {rel} (did it move? update LOCKED_MODULES)"
        code = _code_only(path)
        for line, label, snippet in _scan(code):
            violations.append(f"{rel}:{line}: {label} → {snippet!r}")
    assert not violations, (
        "Contract Kernel Cut Condition B regression — framework-literal dispatch "
        "reintroduced into a LOCKED generation-core module. The core must branch on "
        "the resolved harness / stack contract, never a framework name; move the "
        "framework-specific knowledge into the stack profile/adapter/checker:\n  "
        + "\n  ".join(violations)
    )


def test_framework_lint_catches_a_planted_dispatch() -> None:
    """The lint CATCHES a planted ``framework == "<name>"`` dispatch (no false-RED
    gap): if the de-literalized branch is reintroduced, the gate fails."""
    planted = (
        "def build(framework, is_browser_e2e):\n"
        '    if framework == "Playwright" and is_browser_e2e:\n'
        '        return ["Playwright-specific rules:"]\n'
        "    return []\n"
    )
    hits = _scan(planted)
    assert hits, "lint FAILED to catch a planted `framework == \"Playwright\"` dispatch"
    assert any("dispatch" in label or "literal compare" in label for _, label, _ in hits)

    # also catches the `in (...)` set form and a non-`framework`-LHS framework compare
    assert _scan('    if framework in ("next", "nextjs"):\n        pass\n')
    assert _scan('    if self.framework == "cypress":\n        pass\n')

    # and the hidden-dispatch trap (GPT-5.5 consult): selecting a framework profile
    # BY NAME via a load/registry-resolve call is the same dispatch behind a literal.
    assert _scan('    p = load_addon_profile("playwright")\n')
    assert _scan('    p = framework_registry.resolve("nextjs")\n')


def test_framework_lint_does_not_false_positive_on_prose_or_display() -> None:
    """The lint does NOT false-positive on framework-name CONTENT (guidance prose),
    DISPLAY-string assignments, docstrings/comments, or profile keys.

    These are the legitimate framework-name uses the core keeps — naming a
    framework is fine; *branching on* a framework name is not.
    """
    benign = (
        # guidance prose fed to the model (string literals, no comparison operator)
        '    lines.append("- Do NOT use Playwright/Cypress, do NOT import ' "'@playwright/test'.\")\n"
        '    lines.append("- Browser tests use Playwright `page`, Cypress `cy`.")\n'
        # display-string ASSIGNMENT used for the prompt header (single `=`)
        '    framework = "Playwright"\n'
        '    framework = "pytest"\n'
        # the prompt-header f-string interpolation of the display string
        '    lines.append(f"You are generating executable {framework} test code.")\n'
        # a stack-profile selector KEY/VALUE (data, not a branch)
        '    selectors = {"e2e_harness": "playwright"}\n'
    )
    assert _scan(benign) == [], f"lint false-positived on benign framework-name content: {_scan(benign)}"

    # docstrings/comments naming a framework are stripped, never matched
    with_docs = (
        '"""This module talks about Playwright == the browser harness, framework == X."""\n'
        '# if framework == "Playwright": (this is a COMMENT describing the old code)\n'
        "x = 1\n"
    )
    assert _scan(_code_only_str(with_docs)) == [], (
        "lint false-positived on framework names inside a docstring/comment"
    )


def _code_only_str(src: str) -> str:
    """``_code_only`` for an in-memory string (mirror of the file-based helper)."""
    src = _TRIPLE.sub("\n", src)
    src = _LINE_COMMENT.sub("", src)
    return src


def test_known_unlocked_zone_is_documented_not_silently_ignored() -> None:
    """The legacy ``TestGenerator`` framework-parameterized stub renderer is a
    KNOWN, separately-tracked zone — documented (not silently ignored) and NOT
    claimed as locked, so the gate's scope is honest (mirrors the language gate's
    former ``PENDING_ZONES`` discipline)."""
    assert "e2e_generator.py" in KNOWN_UNLOCKED_FRAMEWORK_ZONES
    # it must NOT be claimed as locked (that would be a false claim, since it still
    # carries the user-parameterized ``self.framework == "cypress"`` dispatch).
    assert "e2e_generator.py" not in LOCKED_MODULES
    # and it genuinely still has framework dispatch today (keeps this honest: if it
    # is ever de-literalized, graduate it into LOCKED_MODULES and drop it here).
    path = _PKG_ROOT / "e2e_generator.py"
    assert path.is_file()
    assert _scan(_code_only(path)), (
        "e2e_generator.py no longer has framework dispatch — graduate it into "
        "LOCKED_MODULES and remove it from KNOWN_UNLOCKED_FRAMEWORK_ZONES."
    )
