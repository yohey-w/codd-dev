"""Deterministic web/HTTP end-to-end harness selection (language-aware).

Historically CoDD's generation pipeline selected a browser E2E harness
(Playwright/TypeScript ``*.spec.ts``) for ANY project whose capability profile
declared ``e2e_modality == "browser"`` — regardless of the project's host
language. A Python Flask web app with an HTTP surface and no node toolchain
therefore honest-failed at verify time with ``Cannot find module
'@playwright/test'``: CoDD chose a TS/node verification harness it had no way to
run. The CLI branch was already language-native ("invoke the built CLI as a
subprocess; do NOT use Playwright"); the web branch was not.

This module is the single, DETERMINISTIC decision point that makes web/HTTP E2E
harness selection language-aware, mirroring the CLI branch. The rules below are
pure functions of ``project_language`` + ``capabilities`` (+ explicit
``constraints``); they NEVER infer intent from LLM prose.

Anti-false-green contract (the load-bearing rule). The Python HTTP downgrade for
a ``browser``-modality project is gated on POSITIVE, structured evidence that
HTTP-contract testing SUFFICES — never on the ABSENCE of a browser flag. An
earlier revision downgraded any Python browser-modality project that lacked an
explicit browser flag to ``pytest_http``; that was itself a false-GREEN, because
a Python web app whose UI/DOM/JS need was expressed only in design PROSE (and not
mirrored into structured constraints) silently got an HTTP-only harness and real
browser evidence was NEVER captured. The browser branch now classifies the
project three ways (:func:`_http_sufficiency`): an explicit browser flag/selector
or a ``user_interface`` capability → the real browser harness; a POSITIVE
http-sufficiency opt-in → ``pytest_http`` for Python; and ``unknown`` (no positive
signal either way) FAILS CLOSED to the browser harness. The worst case for an
ambiguous browser project is therefore a false-RED / explicit node-toolchain
requirement (an honest fail), NEVER a silent false-GREEN.

The matrix is generic (host-language × modality), not Python+Playwright
special-cased: TypeScript/node browser projects keep the existing Playwright
path BYTE-FOR-BYTE; a non-Python HTTP surface keeps today's behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from codd.project_types import ProjectCapabilities


__all__ = ["E2EHarnessSpec", "resolve_e2e_harness"]


#: Config/constraint keys whose VALUE explicitly selects a node/Playwright
#: browser-automation harness. Matching is exact (lower-cased) — prose is never
#: parsed. Keeping this a small allow-list (rather than a substring scan) is what
#: keeps the resolver deterministic and free of LLM-inference.
_EXPLICIT_BROWSER_HARNESS_VALUES = frozenset(
    {"playwright", "playwright_node", "cypress", "browser", "node"}
)

#: Constraint keys that, when truthy, declare browser automation is required.
_EXPLICIT_BROWSER_FLAG_KEYS = frozenset(
    {"browser_automation_required", "requires_browser", "browser_required"}
)

#: Constraint keys whose value is read as an explicit harness selector.
_E2E_HARNESS_SELECTOR_KEYS = frozenset({"e2e_harness", "e2e_runner", "harness"})

#: Constraint keys that, when truthy, are a POSITIVE, structured declaration that
#: HTTP-contract E2E testing is SUFFICIENT for this browser-modality project
#: (i.e. no real-browser/DOM evidence is required — a headless API surface that
#: happens to share the ``browser`` profile). This is the ONLY positive signal
#: that authorises the language-native ``pytest_http`` downgrade for a Python
#: browser-modality project. Matching is exact (lower-cased); prose is NEVER
#: parsed. Absence of this key is NOT a downgrade signal — see
#: :func:`_http_sufficiency` (``unknown`` fails CLOSED toward the browser harness).
_EXPLICIT_HTTP_SUFFICIENCY_FLAG_KEYS = frozenset(
    {"http_contract_sufficient", "http_e2e_sufficient", "no_browser_required"}
)

#: Harness-selector VALUES that explicitly select the Python HTTP-contract
#: harness (the positive mirror of ``_EXPLICIT_BROWSER_HARNESS_VALUES``). An
#: owner who writes ``e2e_harness: pytest_http`` is positively declaring HTTP
#: suffices, just as ``e2e_harness: playwright`` positively declares the browser.
_EXPLICIT_HTTP_HARNESS_VALUES = frozenset({"pytest_http", "http", "pytest"})


@dataclass(frozen=True)
class E2EHarnessSpec:
    """The resolved web/HTTP E2E harness for a project.

    ``runner``    — logical runner id (``pytest_http`` | ``playwright`` |
                    ``native_cli`` | ``none``).
    ``language``  — host language the generated tests are written in.
    ``output_ext``— file extension for generated E2E test files (``.py`` | ``.ts``).
    ``template_ref`` — the verification template the extractor/verify layer
                    routes this harness to (``pytest_http`` | ``playwright`` |
                    ``native_cli`` | ``none``).
    ``requires_node_manifest`` — True when the harness needs a node manifest
                    (``package.json``) to run (Playwright). Consumed by the
                    conditional node-manifest scaffold (deferred) and the
                    toolchain-coherence gate; never inferred from prose.
    """

    runner: str
    language: str
    output_ext: str
    template_ref: str
    requires_node_manifest: bool = False


def _explicit_browser_required(constraints: Mapping[str, Any] | None) -> bool:
    """True ONLY when constraints/config EXPLICITLY declare browser automation.

    Deterministic: a truthy boolean flag key (e.g. ``browser_automation_required:
    true``) OR a harness-selector key whose value names a browser/node runner
    (e.g. ``e2e_harness: playwright_node``). Ambiguous/absent → False. This is the
    ``browser_ui_required`` arm of :func:`_http_sufficiency`; absence is NOT a
    downgrade signal (the ``unknown`` arm fails CLOSED to the browser harness, so a
    missing flag never silently loses browser evidence). LLM prose is NEVER parsed.
    """

    if not isinstance(constraints, Mapping):
        return False

    for key, value in constraints.items():
        if not isinstance(key, str):
            continue
        normalized_key = key.strip().lower()
        if normalized_key in _EXPLICIT_BROWSER_FLAG_KEYS and bool(value):
            return True
        if normalized_key in _E2E_HARNESS_SELECTOR_KEYS and isinstance(value, str):
            if value.strip().lower() in _EXPLICIT_BROWSER_HARNESS_VALUES:
                return True
    return False


def _explicit_http_sufficiency(constraints: Mapping[str, Any] | None) -> bool:
    """True ONLY when constraints/config POSITIVELY declare HTTP E2E suffices.

    Deterministic, structured-only mirror of :func:`_explicit_browser_required`:
    a truthy boolean flag key (e.g. ``http_contract_sufficient: true``) OR a
    harness-selector key whose value names the Python HTTP harness (e.g.
    ``e2e_harness: pytest_http``). This is a POSITIVE opt-in — the owner is
    explicitly stating no real-browser/DOM evidence is required. Ambiguous/absent
    → False. LLM prose is NEVER parsed.
    """

    if not isinstance(constraints, Mapping):
        return False

    for key, value in constraints.items():
        if not isinstance(key, str):
            continue
        normalized_key = key.strip().lower()
        if normalized_key in _EXPLICIT_HTTP_SUFFICIENCY_FLAG_KEYS and bool(value):
            return True
        if normalized_key in _E2E_HARNESS_SELECTOR_KEYS and isinstance(value, str):
            if value.strip().lower() in _EXPLICIT_HTTP_HARNESS_VALUES:
                return True
    return False


def _http_sufficiency(
    constraints: Mapping[str, Any] | None,
    capabilities: ProjectCapabilities,
) -> Literal["browser_ui_required", "http_contract_sufficient", "unknown"]:
    """Classify a BROWSER-modality project from POSITIVE, structured evidence only.

    The decision is driven by POSITIVE evidence, never by the ABSENCE of a flag
    (the historical false-GREEN: a Python web app whose UI/DOM need lived only in
    design PROSE was silently downgraded to HTTP-only ``pytest_http``, so real
    browser evidence was never captured). Three outcomes, in precedence order:

    * ``browser_ui_required`` — an explicit browser flag/harness-selector
      (:func:`_explicit_browser_required`), OR the structured capability signal
      that a user interface is required (``capabilities.user_interface``). Routes
      to the real browser harness (Playwright). The explicit flag has the highest
      precedence: an owner who named a browser harness gets it unconditionally.
    * ``http_contract_sufficient`` — a POSITIVE, structured opt-in declaring HTTP
      E2E suffices (:func:`_explicit_http_sufficiency`): a constraint/config flag
      or an ``e2e_harness: pytest_http`` selector. ONLY this positive signal
      authorises the language-native HTTP downgrade for a Python project.
    * ``unknown`` — NEITHER positive signal present. We do NOT silently downgrade;
      the caller FAILS CLOSED toward the browser harness so browser evidence is
      never lost (worst case a false-RED / explicit node-toolchain requirement,
      NEVER a silent false-GREEN).

    Reads ONLY structured/positive evidence (constraints + the capability
    profile). Prose / LLM inference is never consulted.
    """

    # Explicit browser flag/selector wins outright (strongest explicit signal).
    if _explicit_browser_required(constraints):
        return "browser_ui_required"
    # Positive HTTP-sufficiency opt-in is the ONLY authorised downgrade signal;
    # it is checked before the UI-required capability so an owner can declare an
    # HTTP-only contract for a profile that nominally carries ``user_interface``.
    if _explicit_http_sufficiency(constraints):
        return "http_contract_sufficient"
    # A structured "UI is required" capability is positive browser-need evidence.
    if capabilities.user_interface:
        return "browser_ui_required"
    # No positive signal either way → fail closed toward the browser harness.
    return "unknown"


def resolve_e2e_harness(
    *,
    project_language: str | None,
    capabilities: ProjectCapabilities,
    constraints: Mapping[str, Any] | None = None,
) -> E2EHarnessSpec:
    """Deterministically resolve the E2E harness for a project.

    Rules (pure function of inputs; no prose inference):

    * ``e2e_modality == "cli"`` → native CLI harness (subprocess on the built
      CLI). Output ext follows host language (``.py`` for Python, else ``.ts``).
      The CLI branch already works end-to-end; this is here for completeness so
      the resolver is the single decision point.
    * ``e2e_modality == "browser"`` → a 3-way classification from POSITIVE,
      structured evidence (:func:`_http_sufficiency`) — NEVER from the absence of
      a flag (the historical false-GREEN that silently dropped browser evidence):
        - ``http_contract_sufficient`` (a POSITIVE opt-in that HTTP suffices) AND
          Python → ``pytest_http`` / ``.py``.
        - ``browser_ui_required`` (explicit browser flag/selector, or the
          ``user_interface`` capability) → ``playwright`` / ``.ts`` (node manifest).
        - ``unknown`` (no positive signal) → FAIL CLOSED to ``playwright`` / ``.ts``
          (node manifest). We never silently downgrade an unknown browser project
          to ``pytest_http``: the worst case is a false-RED / explicit
          node-toolchain requirement, NEVER a silent false-GREEN that loses real
          browser/DOM evidence.
    * non-browser HTTP surface (``network_surface == "http"``) → ``pytest_http``
      / ``.py`` for Python; for any other language keep today's behavior by
      routing to the Playwright/node path (``.ts``) so generality holds.
    * otherwise → a ``none`` spec (no E2E layer applies).
    """

    lang = (project_language or "").strip().lower()
    is_python = lang == "python"
    modality = capabilities.e2e_modality

    if modality == "cli":
        return E2EHarnessSpec(
            runner="native_cli",
            language=lang or "python",
            output_ext=".py" if is_python else ".ts",
            template_ref="native_cli",
        )

    if modality == "browser":
        classification = _http_sufficiency(constraints, capabilities)
        # ONLY a POSITIVE http-sufficiency opt-in authorises the Python HTTP
        # downgrade. ``browser_ui_required`` AND ``unknown`` both route to the
        # real browser harness — ``unknown`` fails CLOSED so an undeclared
        # browser project never silently loses DOM evidence (anti-false-green).
        if is_python and classification == "http_contract_sufficient":
            return E2EHarnessSpec(
                runner="pytest_http",
                language="python",
                output_ext=".py",
                template_ref="pytest_http",
            )
        return E2EHarnessSpec(
            runner="playwright",
            language="typescript",
            output_ext=".ts",
            template_ref="playwright",
            requires_node_manifest=True,
        )

    if capabilities.network_surface == "http":
        if is_python:
            return E2EHarnessSpec(
                runner="pytest_http",
                language="python",
                output_ext=".py",
                template_ref="pytest_http",
            )
        # Non-Python HTTP surface: preserve today's TS/Playwright behavior.
        return E2EHarnessSpec(
            runner="playwright",
            language=lang or "typescript",
            output_ext=".ts",
            template_ref="playwright",
            requires_node_manifest=True,
        )

    return E2EHarnessSpec(
        runner="none",
        language=lang or "python",
        output_ext=".py" if is_python else ".ts",
        template_ref="none",
    )
