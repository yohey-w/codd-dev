"""Dataclasses and JSON helpers for app-server benchmark results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunRecord:
    ts: str
    target: str
    backend: str
    transport: str
    concurrency: int
    iteration: int
    duration_seconds: float
    success: bool
    fallback_to_subprocess: bool = False
    error: str | None = None
    stdout_size_bytes: int = 0
    worker_index: int = 0
    attempt: int = 1
    round_duration_seconds: float | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CellResult:
    target: str
    backend: str
    transport: str
    concurrency: int
    rounds: int
    warmup: int
    records: list[RunRecord] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "backend": self.backend,
            "transport": self.transport,
            "concurrency": self.concurrency,
            "rounds": self.rounds,
            "warmup": self.warmup,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "records": [record.to_json() for record in self.records],
        }


@dataclass
class BenchResult:
    created_at: str
    project_root: Path
    output_dir: Path
    raw_jsonl: Path
    summary_markdown: Path
    environment: dict[str, Any]
    cells: list[CellResult] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "project_root": str(self.project_root),
            "output_dir": str(self.output_dir),
            "raw_jsonl": str(self.raw_jsonl),
            "summary_markdown": str(self.summary_markdown),
            "environment": self.environment,
            "cells": [cell.to_json() for cell in self.cells],
        }
