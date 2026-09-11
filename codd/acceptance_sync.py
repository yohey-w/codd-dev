"""Derive the verifiable-behavior registry from the acceptance criteria.

``test_coverage.require_vb_table`` makes an empty VB registry red for a project
that has acceptance criteria to certify. A default-on gate with no way to
satisfy it is hostile, so this module is the other half: a **deterministic**
(no AI, no network) derivation of registry rows from the criteria a project has
already written.

What it produces is a *starting point that is honest about being one*: one VB
row per acceptance criterion, carrying the criterion's own text and its
requirement id. The row is a declaration that the behaviour must be proved; the
proof still has to be written as a test carrying ``codd: covers vb=<id>``.
Deriving the rows mechanically is what keeps the registry and the customer's
acceptance list from drifting apart — they have one source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from codd.acceptance_evidence import AcceptanceCriterion, vb_id_for
from codd.verifiable_behavior_audit import parse_vb_table

CANONICAL_VB_DOC = "docs/test/test_strategy.md"

_HEADER = "| VB ID | Verifiable behavior (from the acceptance criterion) | Requirement |"
_SEPARATOR = "| --- | --- | --- |"

_DOC_PREAMBLE = """---
codd:
  node_id: test:test-strategy
  type: test
  depends_on: []
---

# Test strategy — verifiable behavior registry

This document is the single canonical owner of the verifiable-behavior (VB) id
namespace for this project. Every behavior is declared here exactly once, in the
table below, and is proved by a test carrying a `codd: covers vb=<id>` marker.

The rows are DERIVED from the acceptance criteria in the requirement documents
(`codd acceptance sync`), so the registry and the acceptance list the customer
signed cannot drift apart. Edit the wording freely; keep the ids.
"""


@dataclass(frozen=True)
class SyncResult:
    path: Path
    created: bool
    added: tuple[str, ...]
    already_present: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.created or bool(self.added)


def _single_line(text: str) -> str:
    """Flatten a criterion cell into one table cell (no pipes, no line breaks)."""

    flattened = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    flattened = re.sub(r"<[^>]+>", " ", flattened)
    flattened = flattened.replace("|", "/").replace("\n", " ")
    return re.sub(r"\s+", " ", flattened).strip()


def sync_vb_registry(
    project_root: Path | str,
    criteria: Iterable[AcceptanceCriterion],
    *,
    config: Mapping[str, Any] | None = None,
    doc_path: str = CANONICAL_VB_DOC,
    dry_run: bool = False,
) -> SyncResult:
    """Append a VB row for every acceptance criterion the registry lacks.

    Append-only and id-stable: an existing row is never rewritten or renumbered
    (a VB id is immutable once issued — downstream ``covers`` markers bind to
    it), and a criterion whose id is already declared is reported as such rather
    than duplicated.
    """

    del config  # reserved: doc discovery is pinned to the canonical path
    root = Path(project_root).resolve()
    target = root / doc_path
    existing_text = target.read_text(encoding="utf-8") if target.is_file() else ""
    declared = {behavior.vb_id.casefold() for behavior in parse_vb_table(existing_text)}

    added: list[str] = []
    present: list[str] = []
    rows: list[str] = []
    seen: set[str] = set()
    for criterion in criteria:
        vb_id = vb_id_for(criterion.req_id)
        if not vb_id or vb_id.casefold() in seen:
            continue
        seen.add(vb_id.casefold())
        if vb_id.casefold() in declared:
            present.append(vb_id)
            continue
        added.append(vb_id)
        rows.append(f"| {vb_id} | {_single_line(criterion.text)} | {criterion.req_id} |")

    created = not target.is_file()
    if dry_run or not rows:
        return SyncResult(path=target, created=False, added=tuple(added), already_present=tuple(present))

    if created:
        body = _DOC_PREAMBLE + "\n" + _HEADER + "\n" + _SEPARATOR + "\n" + "\n".join(rows) + "\n"
    elif _HEADER in existing_text:
        body = existing_text.rstrip("\n") + "\n" + "\n".join(rows) + "\n"
    else:
        body = (
            existing_text.rstrip("\n")
            + "\n\n"
            + _HEADER
            + "\n"
            + _SEPARATOR
            + "\n"
            + "\n".join(rows)
            + "\n"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return SyncResult(path=target, created=created, added=tuple(added), already_present=tuple(present))
