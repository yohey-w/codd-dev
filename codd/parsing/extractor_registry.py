"""Registry-DATA-driven extractor SELECTION for ``get_extractor``.

Contract Kernel Cut Condition A (PARSING/EXTRACTION zone). ``get_extractor``
used to select a backend with a ``if normalized_language == "python"`` /
``in _TREE_SITTER_LANGUAGE_PACKAGES`` ladder. This module replaces that with a
DATA table of :class:`ExtractorSpec` entries: the core ``get_extractor`` iterates
the table and picks the first spec whose ``(category, language)`` matches AND
whose capability probe (``available``) is satisfied — NO ``if language ==``
branch in the selection function.

This registry is deliberately BROADER than the greenfield ``default_registry``
(go/python/typescript): the extraction engine analyses many languages (python,
ts, js, go, java, sql, prisma, …), so it must NOT be coupled to the 3 greenfield
profiles. The language NAMES live here in the DATA table, which is the allowed
"registry data" zone (v2.76: "project detection uses registry data").

Capability probes keep graceful degradation intact: a tree-sitter / sql backend
whose optional dependency is missing simply does not match, and selection falls
through to the next spec and finally to :class:`RegexExtractor` (legitimate
best-effort analysis — extraction is NOT a green/red gate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from codd.parsing._shared import LanguageExtractor, RegexExtractor


@dataclass(frozen=True)
class ExtractorSpec:
    """One selectable extractor backend (registry DATA, not name dispatch).

    * ``categories`` / ``languages`` — the ``(category, language)`` pairs this
      spec serves (the NAMES that legitimately live in registry data).
    * ``available`` — capability probe (e.g. tree-sitter binding importable);
      ``None`` means always available.
    * ``factory`` — builds the extractor for a ``(language, category)`` pair.
    """

    categories: frozenset[str]
    languages: frozenset[str]
    factory: Callable[[str, str], LanguageExtractor]
    available: Callable[[str], bool] | None = None

    def matches(self, language: str, category: str) -> bool:
        if category not in self.categories or language not in self.languages:
            return False
        if self.available is not None and not self.available(language):
            return False
        return True


def _build_specs() -> tuple[ExtractorSpec, ...]:
    # Imported lazily inside the builder to avoid import cycles at package load
    # (these backends import from ``_shared`` / each other).
    from codd.parsing.python_ast import PythonAstExtractor
    from codd.parsing.schemas import PrismaSchemaExtractor, SqlDdlExtractor
    from codd.parsing.treesitter import (
        _TREE_SITTER_LANGUAGE_PACKAGES,
        TreeSitterExtractor,
    )

    return (
        # ── schema category ──────────────────────────────────────────────
        ExtractorSpec(
            categories=frozenset({"schema"}),
            languages=frozenset({"sql"}),
            factory=lambda language, category: SqlDdlExtractor(),
            available=lambda language: SqlDdlExtractor.is_available(),
        ),
        ExtractorSpec(
            categories=frozenset({"schema"}),
            languages=frozenset({"prisma"}),
            factory=lambda language, category: PrismaSchemaExtractor(),
        ),
        # ── source category ──────────────────────────────────────────────
        ExtractorSpec(
            categories=frozenset({"source"}),
            languages=frozenset({"python"}),
            factory=lambda language, category: PythonAstExtractor(language, category),
        ),
        # Tree-sitter source backend: serves exactly the languages with a
        # registered binding, gated by the per-language availability probe.
        # NOTE: this mirrors the pre-refactor ladder EXACTLY, including that
        # ``sql`` is a tree-sitter package (so ``get_extractor("sql","source")``
        # selects tree-sitter when available) — byte-identical selection.
        ExtractorSpec(
            categories=frozenset({"source"}),
            languages=frozenset(_TREE_SITTER_LANGUAGE_PACKAGES),
            factory=lambda language, category: TreeSitterExtractor(language, category),
            available=lambda language: TreeSitterExtractor.is_available(language),
        ),
    )


_SPECS: tuple[ExtractorSpec, ...] | None = None


def _specs() -> tuple[ExtractorSpec, ...]:
    global _SPECS
    if _SPECS is None:
        _SPECS = _build_specs()
    return _SPECS


def select_extractor(language: str, category: str = "source") -> LanguageExtractor:
    """Select the best available extractor via the registry-data table.

    Iterates the DATA specs in priority order and returns the first match;
    falls through to :class:`RegexExtractor` (best-effort analysis) when no
    spec serves the ``(language, category)`` pair or the capability is missing.
    """
    normalized_language = (language or "").lower()
    normalized_category = (category or "").lower()
    for spec in _specs():
        if spec.matches(normalized_language, normalized_category):
            return spec.factory(normalized_language, normalized_category)
    return RegexExtractor(normalized_language, normalized_category)
