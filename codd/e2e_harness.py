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
``constraints``); they NEVER infer intent from LLM prose. The North-Star
contract is anti-false-green-safe: when browser automation is NOT explicitly
required, a Python project gets a Python ``pytest`` HTTP E2E harness instead of
TS Playwright. The worst case of preferring HTTP E2E for an ambiguous project is
a false-RED / coverage gap (an honest fail), never a false-GREEN.

The matrix is generic (host-language × modality), not Python+Playwright
special-cased: TypeScript/node browser projects keep the existing Playwright
path BYTE-FOR-BYTE; a non-Python HTTP surface keeps today's behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
    (e.g. ``e2e_harness: playwright_node``). Ambiguous/absent → False, which
    routes Python projects to ``pytest_http`` (at worst a false-RED / coverage
    gap, never a false-GREEN). LLM prose is NEVER parsed.
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
    * ``e2e_modality == "browser"``:
        - Python AND NOT explicit-browser-required → ``pytest_http`` / ``.py``.
        - else → ``playwright`` / ``.ts`` (requires a node manifest).
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
        if is_python and not _explicit_browser_required(constraints):
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
