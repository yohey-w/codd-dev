"""Central access to design-doc structured frontmatter entries.

DAG checks read declarative metadata (``user_journeys``, ``coverage_axes``,
``runtime_constraints``, ``display_fields`` …) off a node's ``attributes``. The
same logical declaration can live in up to three places depending on how the
author wrote it and how the builder/extractor normalized it:

* ``attrs[key]`` — the extractor's normalized top-level copy (lifted from a
  *top-level* frontmatter ``key``).
* ``attrs["frontmatter"][key]`` — the raw top-level frontmatter copy the builder
  keeps verbatim on the node.
* ``attrs["frontmatter"]["codd"][key]`` — the canonical ``codd:`` block position
  the generator emits. The extractor never lifts this into ``attrs[key]``.

A check that reads only ``attrs[key]`` silently ignores anything authored under
the canonical ``frontmatter.codd`` position — a false-green (the declaration is
present but the check behaves as if it were absent / dormant). This helper merges
all three locations so the check sees the union, regardless of where the data was
stashed.

This consolidates the read pattern previously duplicated in
``resource_flow_coherence._entries`` and ``semantic_contract_conflict._section_entries``
(both kept their own copies; this is the shared, reusable form). The de-dup of
the top-level duplicate matches that prior, proven behavior:

* ``attrs["frontmatter"][key]`` is the *same* logical declaration as the lifted
  ``attrs[key]``. Reading both would count it twice (verdict unchanged, but the
  violation/warning counts double). So the raw top-level frontmatter copy is read
  ONLY when the extractor did not already populate ``attrs[key]`` — that still
  covers builder shapes that store only the raw frontmatter without lifting it.
* ``attrs["frontmatter"]["codd"][key]`` is a genuinely *separate* location the
  extractor never lifts, so it is ALWAYS read — preserving union semantics: a
  top-level declaration plus a *different* ``frontmatter.codd`` declaration are
  merged, not deduped away.

The helper is vocabulary-free: it never inspects entry contents or matches any
project/framework/language token. It only locates ``key``-shaped lists of
mappings, so it stays language-free and overfit-free.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def collect_structured_entries(attrs: Any, key: str) -> list[dict[str, Any]]:
    """Return the merged list of mapping entries declared under ``key``.

    Merges ``attrs[key]`` + ``attrs["frontmatter"][key]`` +
    ``attrs["frontmatter"]["codd"][key]`` with the top-level dedup described in
    the module docstring. Non-list values and non-mapping items are ignored
    (never coerced). Returns a list of plain ``dict`` entries (each entry object
    is returned as-is, not copied).
    """

    if not isinstance(attrs, Mapping):
        return []

    top_level = attrs.get(key)
    values: list[Any] = [top_level]

    frontmatter = attrs.get("frontmatter")
    if isinstance(frontmatter, Mapping):
        # Read the raw top-level frontmatter copy only when the extractor did not
        # already lift it into attrs[key] (avoids the double-count); still covers
        # frontmatter-only builder shapes that never lifted it.
        if not (isinstance(top_level, list) and top_level):
            values.append(frontmatter.get(key))
        codd_meta = frontmatter.get("codd")
        if isinstance(codd_meta, Mapping):
            # A genuinely separate location the extractor never lifts → always read.
            values.append(codd_meta.get(key))

    entries: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, list):
            entries.extend(item for item in value if isinstance(item, dict))
    return entries
