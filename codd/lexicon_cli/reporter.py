"""Coverage matrix reporter for lexicon plug-ins."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codd.lexicon_cli.inspector import LexiconInspector
from codd.lexicon_cli.manager import LexiconManager


@dataclass(frozen=True)
class CoverageRow:
    lexicon_id: str
    lexicon_name: str
    axis_type: str
    status: str
    hit_count: int


@dataclass(frozen=True)
class CoverageMatrixReport:
    project_root: str
    generated_at: str
    mode: str
    rows: tuple[CoverageRow, ...]
    totals: dict[str, Any]


class CoverageReporter:
    def __init__(self, project_root: Path | str, lexicon_root: Path | str | None = None):
        self.project_root = Path(project_root).resolve()
        self.manager = LexiconManager(self.project_root, lexicon_root)
        self.inspector = LexiconInspector(self.project_root, lexicon_root)

    def build(
        self,
        lexicons: str | list[str] | tuple[str, ...] = "all",
        *,
        with_ai: bool = False,
        ai_command: Any | None = None,
    ) -> CoverageMatrixReport:
        lexicon_ids = self.resolve_lexicon_ids(lexicons)
        rows: list[CoverageRow] = []
        for lexicon_id in lexicon_ids:
            result = self.inspector.inspect(lexicon_id, with_ai=with_ai, ai_command=ai_command)
            rows.extend(
                CoverageRow(
                    lexicon_id=result.lexicon_id,
                    lexicon_name=result.lexicon_name,
                    axis_type=axis.axis_type,
                    status=axis.status,
                    hit_count=axis.hit_count,
                )
                for axis in result.axes
            )

        counts = Counter(row.status for row in rows)
        total = len(rows)
        covered = sum(count for status, count in counts.items() if status != "unknown")
        return CoverageMatrixReport(
            project_root=self.project_root.as_posix(),
            generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            mode="with-ai" if with_ai else "text-grep",
            rows=tuple(rows),
            totals={
                "lexicons": len(lexicon_ids),
                "axes": total,
                "covered": covered,
                "unknown": counts.get("unknown", 0),
                "status_counts": dict(sorted(counts.items())),
                "covered_pct": round((covered / total) * 100, 2) if total else 0.0,
            },
        )

    def resolve_lexicon_ids(self, lexicons: str | list[str] | tuple[str, ...]) -> list[str]:
        if isinstance(lexicons, str):
            selector = lexicons.strip()
            if not selector or selector == "all":
                installed = self.manager.installed_ids()
                if installed:
                    return installed
                return [record.id for record in self.manager.available()]
            return [item.strip() for item in selector.split(",") if item.strip()]
        return [str(item).strip() for item in lexicons if str(item).strip()]
