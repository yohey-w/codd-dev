"""Prisma obligation checker — the ENFORCEMENT behind the Prisma addon's declared
``client_in_sync_with_schema`` obligation (WARN), so it is not declarative theater.

Prisma generates a client from ``schema.prisma`` and writes a copy of that schema
into the generated client (``node_modules/.prisma/client/schema.prisma``). If the
live schema differs from the generated copy — or the client was never generated —
the client is stale and ``prisma generate`` is needed.

This is a WARN advisory and is biased toward anti-false-RED: when the generated
client carries no detectable schema copy, sync cannot be determined, so it returns
NO finding rather than guessing. The one unambiguous case it always flags is "schema
present but client never generated".
"""

from __future__ import annotations

from pathlib import Path

from ._base import ObligationFinding

_SCHEMA_LOCATIONS = ("prisma/schema.prisma", "schema.prisma")
_GENERATED_SCHEMA_COPIES = (
    "node_modules/.prisma/client/schema.prisma",
    "node_modules/@prisma/client/schema.prisma",
)
_CLIENT_DIRS = ("node_modules/.prisma/client", "node_modules/@prisma/client")


def _find_schema(root: Path) -> Path | None:
    for rel in _SCHEMA_LOCATIONS:
        p = root / rel
        if p.is_file():
            return p
    pdir = root / "prisma"
    if pdir.is_dir():
        for p in sorted(pdir.glob("*.prisma")):
            return p
    return None


def _normalize(text: str) -> str:
    """Whitespace-insensitive view so trivial reformatting is not flagged as drift."""
    return "\n".join(
        line.rstrip() for line in text.replace("\r\n", "\n").split("\n")
    ).strip()


def check_schema_sync(project_root: str | Path, **_: object) -> list[ObligationFinding]:
    """``client_in_sync_with_schema`` (WARN): the generated Prisma client must match
    the live ``schema.prisma``.

    Returns a finding when the client is not generated, or when its embedded schema
    copy differs from the live schema. Returns [] when there is no Prisma schema
    (nothing to assert) or when the generated client exposes no schema copy to
    compare against (sync undeterminable — no false-RED)."""
    root = Path(project_root)
    schema = _find_schema(root)
    if schema is None:
        return []  # no Prisma schema in this project → nothing to assert
    if not any((root / d).is_dir() for d in _CLIENT_DIRS):
        return [
            ObligationFinding(
                obligation_id="client_in_sync_with_schema",
                location="node_modules/.prisma/client",
                detail="Prisma client is not generated — run `prisma generate`",
            )
        ]
    live = _normalize(schema.read_text(encoding="utf-8", errors="replace"))
    for rel in _GENERATED_SCHEMA_COPIES:
        copy = root / rel
        if copy.is_file():
            generated = _normalize(copy.read_text(encoding="utf-8", errors="replace"))
            if generated != live:
                return [
                    ObligationFinding(
                        obligation_id="client_in_sync_with_schema",
                        location=str(schema.relative_to(root)),
                        detail=(
                            "generated Prisma client is out of sync with "
                            "schema.prisma — run `prisma generate`"
                        ),
                    )
                ]
            return []  # embedded copy matches the live schema → in sync
    return []  # client present but no embedded schema copy → undeterminable, no false-RED
