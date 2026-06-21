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

Cut Condition A is now COMPLETE for the static language-literal gate: the LAST
four pending zones have graduated into LOCKED_MODULES (so PENDING_ZONES is empty
— see ``test_cut_a_static_language_gate_is_complete``). Their de-literalization:
  * codd/repair_slice.py          — the repair line-range/raises analyzer is
    REGISTRY-DATA driven (``regex_strategies.repair_slice_profile_for`` →
    ``RepairSliceLanguageProfile``: the tree-sitter func-node-type SET, the
    regex def-vs-function pattern + name group, the python-only raises regex), no
    ``language in ("typescript",…)`` / ``language == "python"`` branch. (A latent
    ``ext._get_parser()`` bug — no such method; ``TreeSitterExtractor`` exposes
    ``_parse`` — was fixed in the same pass, activating the ts/js tree-sitter walk.)
  * codd/implementer.py           — generation ``is_python`` is driven from the
    declared extensions (``.py`` in ``_implementation_language_extensions``); the
    UI/JSX-variant extension decision is a capability-DATA flag
    (``_ui_variant_extension``), no ``language in {"typescript","javascript"}``.
  * codd/e2e_harness.py           — the ``is_python`` modality routing reads the
    canonical-keyed extension registry (``.py`` in ``language_extensions``).
  * codd/vb_marker_authenticity.py — the poetry-manifest reserved-key filter (a
    GATE input) keys on a named DATA constant
    (``_POETRY_RESERVED_NON_DEPENDENCY_KEYS``): ``python`` under
    ``[tool.poetry.dependencies]`` is the interpreter pin (a poetry FILE-FORMAT
    fact), not a target-language dispatch — byte-identical to ``!= "python"``.

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
    # graduated (Contract Kernel — Cut Condition A FINAL increment): the LAST
    # four forbidden-zone files, now language-free. With these locked the Cut A
    # static language-literal gate is COMPLETE — no core/forbidden zone carries a
    # language-name literal dispatch anymore (PENDING_ZONES is empty; see
    # ``test_cut_a_static_language_gate_is_complete``).
    #   * repair_slice.py          — the repair-slice function line-range + raises
    #     analyzer. The tree-sitter func-node-type SET, the regex def-vs-function
    #     pattern + name group, and the python-only raises regex are now
    #     REGISTRY-DATA driven (``regex_strategies.repair_slice_profile_for`` →
    #     ``RepairSliceLanguageProfile``), no ``language in ("typescript",…)`` /
    #     ``language == "python"`` branch. (A latent ``ext._get_parser()`` bug —
    #     the method never existed; ``TreeSitterExtractor`` exposes ``_parse`` —
    #     was fixed in the same pass, activating the tree-sitter walk for ts/js.)
    #   * implementer.py           — generation-time ``is_python`` (confusable
    #     check) is driven from the language's declared extensions (``.py`` in
    #     ``_implementation_language_extensions``); the UI-facing / JSX-variant
    #     extension decision is driven from a capability-DATA flag
    #     (``_ui_variant_extension`` ⇒ ``.tsx``/``.jsx`` or ``None``), no
    #     ``language in {"typescript","javascript"}`` branch.
    #   * e2e_harness.py           — the ``is_python`` modality routing is driven
    #     from the canonical-keyed extension registry (``.py`` in
    #     ``regex_strategies.language_extensions``), no ``lang == "python"``.
    #   * vb_marker_authenticity.py — the poetry-manifest reserved-key filter (a
    #     GATE input) keys on a named DATA constant
    #     (``_POETRY_RESERVED_NON_DEPENDENCY_KEYS``) documenting that ``python`` in
    #     ``[tool.poetry.dependencies]`` is the interpreter pin (a poetry
    #     FILE-FORMAT fact), not a target-language dispatch — byte-identical to
    #     the former ``k.lower() != "python"``.
    "repair_slice.py",
    "implementer.py",
    "e2e_harness.py",
    "vb_marker_authenticity.py",
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

# Known-pending Cut Condition A zones. This is now EMPTY: the Cut A static
# language-literal gate is COMPLETE. Every former pending file
# (``repair_slice.py``, ``implementer.py``, ``e2e_harness.py``,
# ``vb_marker_authenticity.py``) graduated into LOCKED_MODULES once its
# language-name DISPATCH was driven from registry/profile DATA (the CEG core —
# ``scanner.py`` + ``extractor.py`` — and ``project_types.py`` graduated in
# earlier increments). No core/forbidden zone carries a language-name literal
# dispatch anymore. ``test_cut_a_static_language_gate_is_complete`` asserts this
# emptiness AND re-verifies the four final files stay clean, so a regression that
# reintroduces a literal into any of them fails loudly. (Cut Condition B — the
# framework-pluggable STACK literal gate — is a SEPARATE concern tracked by its
# own goal section / tests, not this language-gate list.)
PENDING_ZONES: tuple[str, ...] = ()

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


#: The four FINAL Cut Condition A files that graduated in the closing increment.
#: They must stay LOCKED + clean forever (never regress back to "pending").
_CUT_A_FINAL_GRADUATES = (
    "repair_slice.py",
    "implementer.py",
    "e2e_harness.py",
    "vb_marker_authenticity.py",
)


def test_cut_a_static_language_gate_is_complete() -> None:
    """Cut Condition A's static language-literal gate is COMPLETE.

    Replaces the old "pending list must be non-empty" guard: every former pending
    zone has graduated, so ``PENDING_ZONES`` is now empty and there is no
    coverage gap left to document. This test keeps that completion HONEST and
    REGRESSION-PROOF:

    1. ``PENDING_ZONES`` is empty (Cut A static gate done; if you ever re-add a
       pending zone you must also re-add a guard like the old one).
    2. Every file that has graduated (the CEG core, ``project_types.py``, the
       verify-runner, and the four FINAL graduates) is in ``LOCKED_MODULES`` and
       NOT in ``PENDING_ZONES`` — none may silently regress.
    3. The four final graduates re-pass the language-literal scan RIGHT HERE
       (belt-and-suspenders over ``test_oracle_core_modules_are_language_free``):
       if anyone reintroduces a ``language ==`` / ``language in (...)`` /
       ``== "<lang>"`` dispatch into them, this fails loudly and names the file.

    (Cut Condition B — the framework-pluggable STACK literal gate — is a SEPARATE
    concern with its own goal section / tests; this list is the LANGUAGE gate.)
    """

    assert PENDING_ZONES == (), (
        "Cut Condition A static language gate is COMPLETE — PENDING_ZONES must be "
        "empty. If you intentionally re-open a pending zone, restore a "
        "documented-gap guard (the old test) so the coverage gap is never silent."
    )

    must_stay_locked = (
        "repair/verify_runner.py",
        "project_types.py",
        "extractor.py",
        "scanner.py",
        *_CUT_A_FINAL_GRADUATES,
    )
    for graduated in must_stay_locked:
        assert graduated in LOCKED_MODULES, (
            f"{graduated} graduated (registry/profile-data-driven); it must stay in "
            "LOCKED_MODULES."
        )
        assert graduated not in PENDING_ZONES, (
            f"{graduated} is graduated; it must never regress back into PENDING_ZONES."
        )

    # Re-verify the four FINAL graduates are genuinely language-literal-free here,
    # so a regression in any of them is caught by THIS completion test too.
    residual: list[str] = []
    for rel in _CUT_A_FINAL_GRADUATES:
        path = _PKG_ROOT / rel
        assert path.is_file(), f"final graduate missing: {rel}"
        code = _code_only(path)
        for pattern, label in _FORBIDDEN:
            for m in pattern.finditer(code):
                line = code.count("\n", 0, m.start()) + 1
                residual.append(f"{rel}:{line}: {label}")
    assert not residual, (
        "A FINAL Cut Condition A graduate regressed — a language-name literal "
        "dispatch was reintroduced. Drive the decision from the resolved "
        "LanguageProfile / registry DATA, never a language name:\n  "
        + "\n  ".join(residual)
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
