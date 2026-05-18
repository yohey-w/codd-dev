"""JSONL and Markdown reporting for benchmark runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .aggregator import aggregate_cells
from .schema import BenchResult


def write_jsonl(result: BenchResult) -> Path:
    result.raw_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with result.raw_jsonl.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "environment",
                    "created_at": result.created_at,
                    "project_root": str(result.project_root),
                    **result.environment,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        for cell in result.cells:
            for record in cell.records:
                handle.write(
                    json.dumps({"type": "record", **record.to_json()}, ensure_ascii=False, sort_keys=True)
                    + "\n"
                )
            if cell.skipped:
                handle.write(
                    json.dumps(
                        {
                            "type": "cell_skipped",
                            "target": cell.target,
                            "backend": cell.backend,
                            "transport": cell.transport,
                            "concurrency": cell.concurrency,
                            "reason": cell.skip_reason,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
    return result.raw_jsonl


def write_markdown(result: BenchResult) -> Path:
    result.summary_markdown.parent.mkdir(parents=True, exist_ok=True)
    result.summary_markdown.write_text(render_markdown(result), encoding="utf-8")
    return result.summary_markdown


def render_markdown(result: BenchResult) -> str:
    summaries = aggregate_cells(result.cells)
    lines = [
        "# cmd_358 - Codex App Server vs Subprocess Benchmark",
        "",
        "## Run",
        "",
        f"- Created: {result.created_at}",
        f"- Project root: `{result.project_root}`",
        f"- Raw JSONL: `{result.raw_jsonl}`",
        "",
        "## Environment",
        "",
    ]
    for key, value in sorted(result.environment.items()):
        lines.append(f"- {key}: `{value}`")

    lines.extend(
        [
            "",
            "## Summary",
            "",
            "| target | backend | transport | concurrency | records | success | error_rate | fallback_rate | P50 | P95 | P99 | mean | max | throughput |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for summary in summaries:
        lines.append(
            "| {target} | {backend} | {transport} | {concurrency} | {records} | {success_count} | "
            "{error_rate} | {fallback_rate} | {p50} | {p95} | {p99} | {mean} | {max} | {throughput} |".format(
                target=summary["target"],
                backend=summary["backend"],
                transport=summary["transport"],
                concurrency=summary["concurrency"],
                records=summary["records"],
                success_count=summary["success_count"],
                error_rate=_pct(summary["error_rate"]),
                fallback_rate=_pct(summary["fallback_rate"]),
                p50=_seconds(summary["p50"]),
                p95=_seconds(summary["p95"]),
                p99=_seconds(summary["p99"]),
                mean=_seconds(summary["mean"]),
                max=_seconds(summary["max"]),
                throughput=_number(summary["throughput_req_per_sec"]),
            )
        )

    skipped = [summary for summary in summaries if summary["skipped"]]
    if skipped:
        lines.extend(["", "## Skipped Cells", ""])
        for summary in skipped:
            lines.append(
                f"- {summary['target']} / {summary['backend']} / concurrency={summary['concurrency']}: "
                f"{summary['skip_reason']}"
            )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Fill in interpretation after real measurements are run with Lord approval.",
            "- Fallback rate above 10% indicates an unhealthy app-server benchmark environment.",
        ]
    )
    return "\n".join(lines) + "\n"


def _seconds(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.3f}s"


def _pct(value: Any) -> str:
    return f"{float(value) * 100:.1f}%"


def _number(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"
