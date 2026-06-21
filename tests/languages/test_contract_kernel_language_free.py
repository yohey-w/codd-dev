"""Static gate (Contract Kernel oracle dispatch §10): the oracle-dispatch CORE
stays language-free — FOREVER.

The Contract Kernel's cardinal architecture rule: the core branches on NO
language (or framework) name. All language knowledge lives in the declarative
profiles (``codd/languages/profiles/*.yaml``) and the per-language adapters
(``codd/languages/adapters/oracle_*.py`` — those are SUPPOSED to know their
language; they are NOT core and are excluded here). v3.0 Cut Condition A = the
forbidden-zone core has zero language-literal dispatch.

This test FAILS the moment a language-name literal dispatch (``language == "go"``,
``language in ("typescript", "node")``, ``default_registry.resolve("python")``,
the removed ``_REGISTRY_COMPOSITE_ORACLE_LANGUAGES`` allowlist, …) is reintroduced
into a LOCKED core module, so the Cut Condition A achievement for the oracle zone
cannot silently regress (steps 5-9 removed every such literal; this keeps them out).

LOCKED zones (asserted language-free here — the oracle dispatch + verify contract
kernel, all cleaned by Contract Kernel steps 5-9; plus the verify-stage
node-install + default-tsc heuristic, made profile-driven and graduated here):
  * codd/implement_oracle.py          — the implement-oracle gate + dispatch
  * codd/languages/oracle_executor.py — the generic command-sequence executor
  * codd/languages/verify_executor.py — the verify contract executor
  * codd/languages/verify_plan.py     — the verify plan builder
  * codd/repair/verify_runner.py      — ``_is_node_project`` / the install-preflight
    + default-typecheck gate, now PROFILE-DRIVEN (the profile's ``typecheck``
    ``requires_materialized_deps`` + ``materialize_command``), no language-name literal.

NOT YET locked (Cut Condition A still pending for these zones — listed so the
coverage gap is EXPLICIT, never silently uncovered; each graduates into
LOCKED_MODULES when it is made contract-driven):
  * codd/project_types.py, codd/detection/* (stack_detector / import_coherence),
    the PathPlanner / path_rules zone.

Dynamic escape coverage (a SEEDED incoherence must reach RED — escape == 0) is
asserted by the per-language anti-false-green suites, which TOGETHER guarantee no
seeded incoherence escapes to a false GREEN:
  * test_oracle_go_adapter / test_oracle_go_parity  — undefined symbol, first-party
    miss, vet diagnostic, the ``ok/``-named-package summary-regex false-green, the
    third-party ``go test`` envelope tolerance.
  * test_oracle_python_parity                       — compile error, first-party
    missing import, pytest-collect import error, pytest-missing → environment RED.
  * test_oracle_typescript_parity                   — TS2305/2307, the TS18003-on-rc0
    "passed but compiled nothing" false-green guard.
  * test_oracle_synthetic_language                  — a NEW language's broken oracle
    → RED with NO core change (the generality proof).
  * test_oracle_unsupported_red                     — a declared-but-unsupported
    stack → RED, never a silent NO-OP pass.
"""

from __future__ import annotations

import re
from pathlib import Path

import codd

# ── the locked core modules (relative to the package root) ───────────────────
LOCKED_MODULES = (
    "implement_oracle.py",
    "languages/oracle_executor.py",
    "languages/verify_executor.py",
    "languages/verify_plan.py",
    "repair/verify_runner.py",  # graduated: _is_node_project is now profile-driven
)

# Known-pending Cut Condition A zones — documented so the gap is explicit (NOT
# asserted clean; they still contain language literals by design-debt).
PENDING_ZONES = (
    "project_types.py",  # _LAYOUT_PROFILE_BUILDERS keys + language == "python"/... dispatch
    "detection",
    "path_rules / PathPlanner",
)

_PKG_ROOT = Path(codd.__file__).resolve().parent

# Language-name literal DISPATCH idioms forbidden in the core (the patterns steps
# 5-9 eliminated). Matched against CODE ONLY (docstrings + comments stripped), so
# prose that *describes* these idioms (this repo's own architecture docs) is fine.
_FORBIDDEN = (
    (re.compile(r"\blanguage\b\s*(?:==|!=)\s*['\"]"), "language == \"<lang>\" literal compare"),
    (re.compile(r"\blanguage\b\s+(?:not\s+)?in\s*[\(\[]"), "language in (...) literal set dispatch"),
    (
        re.compile(r"""\.resolve\(\s*['"](?:go|golang|python|py|typescript|node|ts|js)['"]"""),
        "hardcoded registry resolve(\"<lang>\")",
    ),
    (
        re.compile(r"""(?<![\w.])(?:==|!=)\s*['"](?:go|golang|python|typescript|node)['"]"""),
        "== \"<lang>\" literal compare (e.g. profile.language == \"go\")",
    ),
    (re.compile(r"_REGISTRY_COMPOSITE_ORACLE"), "the removed composite-oracle language allowlist"),
)

# Strip triple-quoted docstrings/blocks and # line-comments so explanatory PROSE
# (which legitimately mentions these idioms) is not matched — only live CODE is.
_TRIPLE = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)
_LINE_COMMENT = re.compile(r"#.*?$", re.MULTILINE)


def _code_only(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    src = _TRIPLE.sub("\n", src)
    src = _LINE_COMMENT.sub("", src)
    return src


def test_oracle_core_modules_are_language_free() -> None:
    """Every LOCKED core module is free of language-name literal dispatch."""
    violations: list[str] = []
    for rel in LOCKED_MODULES:
        path = _PKG_ROOT / rel
        assert path.is_file(), f"locked module missing: {rel} (did it move? update LOCKED_MODULES)"
        code = _code_only(path)
        for pattern, label in _FORBIDDEN:
            for m in pattern.finditer(code):
                line = code.count("\n", 0, m.start()) + 1
                snippet = code[m.start() : m.start() + 60].splitlines()[0].strip()
                violations.append(f"{rel}:{line}: {label} → {snippet!r}")
    assert not violations, (
        "Contract Kernel Cut Condition A regression — language-literal dispatch "
        "reintroduced into a LOCKED core module. The core must branch on a "
        "ResolvedLanguageContract + registered adapter capability, never a language "
        "name; move this knowledge into the profile/adapter:\n  " + "\n  ".join(violations)
    )


def test_pending_zones_are_documented_not_silently_uncovered() -> None:
    """The not-yet-locked Cut Condition A zones are explicitly listed.

    A guard against silent scope-narrowing: if the headline pending zone's language
    literal is ever cleaned, it should GRADUATE into LOCKED_MODULES (and drop from
    PENDING_ZONES) — this asserts the pending list is non-empty until Cut Condition
    A is fully done, so the coverage gap is never quietly forgotten.

    The headline pending zone is now ``project_types.py`` (``verify_runner.py``
    graduated into LOCKED_MODULES once ``_is_node_project`` became profile-driven). It
    really does still carry ``language == "python"`` / ``language in (...)`` dispatch in
    its ``_LAYOUT_PROFILE_BUILDERS`` / ``test_block_profile`` / scaffold paths — keeping
    the documentation honest: if someone cleans it without updating this file, this
    fails and prompts the next graduation.
    """
    assert PENDING_ZONES, "if Cut Condition A is fully done, lock all zones + remove this test"
    # verify_runner.py must NOT regress back into PENDING_ZONES — it is locked now.
    assert "repair/verify_runner.py" not in PENDING_ZONES, (
        "repair/verify_runner.py is graduated (profile-driven); it must stay in "
        "LOCKED_MODULES, never back in PENDING_ZONES."
    )
    project_types = _PKG_ROOT / "project_types.py"
    if project_types.is_file():
        assert re.search(r"""\blanguage\b\s+in\s*\(""", _code_only(project_types)), (
            "project_types.py no longer keys on a language literal — graduate "
            "project_types.py into LOCKED_MODULES and drop it from PENDING_ZONES "
            "(repoint this assertion to the next pending file: detection / path_rules)."
        )
