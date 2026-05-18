"""Statistics aggregation for app-server benchmark results."""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable

from .schema import CellResult, RunRecord


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    data = sorted(float(value) for value in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = math.ceil((percentile_value / 100.0) * len(data)) - 1
    index = min(max(rank, 0), len(data) - 1)
    return data[index]


def aggregate_cell(cell: CellResult) -> dict[str, Any]:
    records = list(cell.records)
    successful = [record.duration_seconds for record in records if record.success]
    total = len(records)
    success_count = len(successful)
    error_count = total - success_count
    fallback_count = sum(1 for record in records if record.fallback_to_subprocess)
    throughput = _throughput(records)

    return {
        "target": cell.target,
        "backend": cell.backend,
        "transport": cell.transport,
        "concurrency": cell.concurrency,
        "rounds": cell.rounds,
        "warmup": cell.warmup,
        "records": total,
        "success_count": success_count,
        "error_count": error_count,
        "error_rate": (error_count / total) if total else 0.0,
        "fallback_count": fallback_count,
        "fallback_rate": (fallback_count / total) if total else 0.0,
        "rate_limited_count": sum(1 for record in records if record.error == "rate_limited"),
        "p50": percentile(successful, 50),
        "p95": percentile(successful, 95),
        "p99": percentile(successful, 99),
        "mean": statistics.fmean(successful) if successful else None,
        "max": max(successful) if successful else None,
        "throughput_req_per_sec": throughput,
        "skipped": cell.skipped,
        "skip_reason": cell.skip_reason,
    }


def aggregate_cells(cells: Iterable[CellResult]) -> list[dict[str, Any]]:
    return [aggregate_cell(cell) for cell in cells]


def load_records_jsonl(path: Path) -> list[RunRecord]:
    records: list[RunRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            payload = json.loads(line)
            if payload.get("type") != "record":
                continue
            data = dict(payload)
            data.pop("type", None)
            records.append(RunRecord(**data))
    return records


def _throughput(records: list[RunRecord]) -> float | None:
    successful = [record for record in records if record.success]
    if not successful:
        return None
    round_durations: dict[int, float] = {}
    for record in records:
        duration = record.round_duration_seconds or record.duration_seconds
        round_durations[record.iteration] = max(round_durations.get(record.iteration, 0.0), duration)
    total_wall_seconds = sum(round_durations.values())
    if total_wall_seconds <= 0:
        return None
    return len(successful) / total_wall_seconds
