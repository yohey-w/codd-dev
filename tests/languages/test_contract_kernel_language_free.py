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
  * codd/project_types.py             — the scaffold/layout/test-block/ensurer
    dispatch, now PROFILE-DRIVEN (Contract Kernel v2.71): routed by the resolved
    ``LanguageProfile``'s ``layout.package_root.kind`` SHAPE + the
    ``tests.semantics_adapter`` capability id, no ``self.language == ...`` /
    language-name-keyed builder dict.
  * codd/extractor.py                 — the CEG fact-extraction core
    (``_extract_symbols`` / ``_extract_imports`` / ``_detect_code_patterns`` /
    ``_common_stdlib`` / ``_file_to_module`` / ``_guess_test_target`` /
    ``_language_extensions`` / entry-point map), now REGISTRY-DATA-driven: it
    dispatches through ``codd.parsing.regex_strategies.strategy_for(language)``
    (+ ``language_extensions`` / ``common_stdlib`` / ``entry_point_candidates``)
    and ``codd.parsing.extractor_registry.select_extractor`` for backend
    selection — no inline ``if language ==`` ladder.
  * codd/scanner.py                   — the CEG source-dependency scanner
    (``_extract_imports_basic`` / ``_scan_source_directory``), now
    REGISTRY-DATA-driven: ``ceg_import_targets(language, …)`` +
    ``language_extensions(language)``, no ``language in (...)`` dispatch.

ALLOWED extractor-IMPLEMENTATION zone (the ``parsing/**`` analogue of
``languages/adapters/**`` — see ALLOWED_IMPL_ZONES): the per-language regex /
tree-sitter / sql / prisma extractor implementations AND the language→strategy /
language→extractor selection tables. An extractor implementation legitimately
knows its OWN language (like an adapter), so language NAMES live there as
registry DATA. Extraction here is ANALYSIS input (it populates the
CEG/ProjectFacts), never a green/red GATE verdict.

NOT YET locked (Cut Condition A still pending for these zones — listed so the
coverage gap is EXPLICIT, never silently uncovered; each graduates into
LOCKED_MODULES when it is made contract-driven). Each is a SEPARATE concern from
the CEG extraction core:
  * codd/repair_slice.py (repair line-range/raises analyzer language dispatch),
    codd/implementer.py (generation extension choice), codd/e2e_harness.py,
    codd/vb_marker_authenticity.py (v2.72 adapter-migration zone).

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
    # graduated (Contract Kernel v2.71): the scaffold/layout/test-block/ensurer
    # dispatch is now PROFILE-DRIVEN (resolved LanguageProfile's
    # ``layout.package_root.kind`` SHAPE + ``tests.semantics_adapter`` capability
    # id), no ``self.language == ...`` / language-name-keyed builder dict.
    "project_types.py",
    # graduated (Contract Kernel — PARSING/EXTRACTION zone): the CEG extraction
    # engine. ``extractor.py``'s core functions (``_extract_symbols``,
    # ``_extract_imports``, ``_detect_code_patterns``, ``_common_stdlib``,
    # ``_file_to_module``, ``_guess_test_target``, ``_language_extensions``,
    # entry-point map) and ``scanner.py``'s ``_extract_imports_basic`` /
    # ``_scan_source_directory`` are now REGISTRY-DATA-driven: they dispatch
    # through ``codd.parsing.regex_strategies.strategy_for(language)`` /
    # ``ceg_import_targets`` / ``language_extensions`` and
    # ``codd.parsing.extractor_registry.select_extractor`` — no inline
    # ``if language ==`` / ``language in (...)`` ladder. The per-language regex
    # bodies + the language→extractor selection table live in the ALLOWED
    # extractor-implementation zone (``codd/parsing/**``, see ALLOWED_IMPL_ZONES).
    "extractor.py",
    "scanner.py",
)

# Extractor-IMPLEMENTATION zones (the parsing/** analogue of
# ``languages/adapters/**``). An extractor implementation legitimately knows its
# OWN language — exactly like an adapter knows its language — so language NAMES
# are allowed here as registry DATA (the language→strategy / language→extractor
# tables, and the per-language regex/tree-sitter bodies). The Contract Kernel
# rule forbids language-name DISPATCH in the CORE; it does NOT forbid an
# extractor implementation (or a profile/adapter) from naming its own language.
# This mirrors how ``codd/languages/adapters/**`` is excluded from the locked
# oracle/verify core. Extraction here is ANALYSIS input (it populates the
# CEG/ProjectFacts), never a green/red GATE verdict, so a best-effort regex
# fallback for an unknown language is legitimate analysis, not a false-green.
ALLOWED_IMPL_ZONES = (
    "parsing",  # codd/parsing/** — the per-language extractor implementations
)

# Known-pending Cut Condition A zones — documented so the gap is explicit (NOT
# asserted clean; they still contain language literals by design-debt).
# ``scanner.py`` + ``extractor.py`` graduated into LOCKED_MODULES once the CEG
# source-extraction dispatch became REGISTRY-DATA driven (Contract Kernel
# PARSING/EXTRACTION zone). The remaining pending files each carry a real
# language literal today (verified by the graduation test below); they are
# SEPARATE concerns from the CEG extraction core and graduate in their own
# follow-up increments:
#   * repair_slice.py          — repair-slice line-range/raises analyzer still
#                                keys on ``language in ("typescript","javascript")``
#                                in its tree-sitter/regex fallback (7 literals).
#   * implementer.py           — generation extension choice ``language=="python"``.
#   * e2e_harness.py           — e2e harness ``lang=="python"`` branch.
#   * vb_marker_authenticity.py — belongs to the v2.72 adapter-migration zone.
PENDING_ZONES = (
    "repair_slice.py",
    "implementer.py",
    "e2e_harness.py",
    "vb_marker_authenticity.py",
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

    The headline pending zone is now ``repair_slice.py`` (the CEG extraction
    core — ``extractor.py`` + ``scanner.py`` — graduated into LOCKED_MODULES once
    its dispatch became REGISTRY-DATA driven; ``project_types.py`` graduated at
    v2.71). ``repair_slice.py`` really does still carry
    ``language in ("typescript", "javascript")`` dispatch in its line-range /
    raises analyzer — keeping the documentation honest: if someone cleans it
    without updating this file, this fails and prompts the next graduation.
    """
    assert PENDING_ZONES, "if Cut Condition A is fully done, lock all zones + remove this test"
    # verify_runner.py must NOT regress back into PENDING_ZONES — it is locked now.
    assert "repair/verify_runner.py" not in PENDING_ZONES, (
        "repair/verify_runner.py is graduated (profile-driven); it must stay in "
        "LOCKED_MODULES, never back in PENDING_ZONES."
    )
    # project_types.py must NOT regress back into PENDING_ZONES — it is locked now
    # (Contract Kernel v2.71 de-literalization).
    assert "project_types.py" not in PENDING_ZONES, (
        "project_types.py is graduated (profile-driven scaffold/layout/test-block "
        "dispatch); it must stay in LOCKED_MODULES, never back in PENDING_ZONES."
    )
    # extractor.py / scanner.py must NOT regress back into PENDING_ZONES — the
    # CEG extraction engine is locked now (registry-data-driven dispatch).
    for graduated in ("extractor.py", "scanner.py"):
        assert graduated not in PENDING_ZONES, (
            f"{graduated} is graduated (registry-data-driven extraction); it must "
            "stay in LOCKED_MODULES, never back in PENDING_ZONES."
        )
    # The headline pending zone's literal really still exists (documentation
    # honesty): if it is cleaned, graduate it and repoint this assertion.
    repair_slice = _PKG_ROOT / "repair_slice.py"
    if repair_slice.is_file():
        assert re.search(r"""\blanguage\b\s+in\s*\(""", _code_only(repair_slice)), (
            "repair_slice.py no longer keys on a language literal — graduate it "
            "into LOCKED_MODULES and drop it from PENDING_ZONES (repoint this "
            "assertion to the next pending file: implementer.py / e2e_harness.py)."
        )


def test_parsing_is_an_allowed_extractor_impl_zone_not_locked() -> None:
    """``codd/parsing/**`` is the extractor-IMPLEMENTATION zone (allowed), not core.

    Mirrors how ``languages/adapters/**`` is excluded from the locked oracle/verify
    core: an extractor implementation legitimately knows its OWN language, so the
    per-language regex/tree-sitter bodies + the language→strategy / language→extractor
    registry tables live in ``parsing/**`` as DATA. They must therefore NOT be in
    LOCKED_MODULES (that would forbid the very data table the de-literalization
    moved the names INTO). This asserts the allowed zone is real and that no
    parsing/** file was accidentally locked.
    """
    assert "parsing" in ALLOWED_IMPL_ZONES
    for rel in LOCKED_MODULES:
        assert not rel.startswith("parsing/"), (
            f"{rel} is in the ALLOWED extractor-implementation zone (parsing/**); "
            "it must NOT be locked — the language names there are registry DATA "
            "(analogous to languages/adapters/**), not core dispatch."
        )


def test_extraction_language_names_live_in_registry_data_not_deleted() -> None:
    """De-literalization moved language NAMES into registry DATA (didn't delete).

    The Cut Condition A goal is to remove language-name DISPATCH from the core,
    NOT to drop language support. This proves the supported language names now
    live in the ALLOWED registry-data tables (``regex_strategies._STRATEGIES`` +
    ``extractor_registry``), so the locked-core cleanliness is a genuine
    relocation, not a silent capability loss.
    """
    from codd.parsing import regex_strategies as rs

    for language in ("python", "typescript", "javascript", "go", "java"):
        assert language in rs._STRATEGIES, language
        # the strategy carries real per-language behavior (data), not a stub
        strat = rs.strategy_for(language)
        assert strat.name == language
        assert strat.extensions, language

    # unknown language → generic best-effort strategy (analysis, never a crash)
    generic = rs.strategy_for("cobol")
    assert generic is rs.GENERIC_STRATEGY
    assert generic.symbols("x", "f") == []
    assert generic.extensions == frozenset()
