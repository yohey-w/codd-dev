#!/usr/bin/env python3
"""cmd_364: Direct AiCommand benchmark for subprocess vs Codex App Server.

このスクリプトは codd の deployment.providers.ai_command_factory を直接叩いて
N 回連続 invoke した実測値を取る。codd implement/verify/fix の orchestration
オーバーヘッドを除いた "AI call layer" 専用の測定であり、per_session 償却
効果を最もクリーンに観察できる。

cmd_358 の bench_app_server.py が codd 全体を invoke するため codd 自身の
overhead や mock_repair_engine 経路 (LLM 不経由) が混ざる弱点を補完する。

Usage:
    python3 scripts/bench_app_server_direct.py \
        --backend both \
        --concurrency 1,10,30 \
        --turns-per-invocation 3 \
        --invocations 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import statistics
import sys
import time
from typing import Iterable

from codd.deployment.providers.ai_command_factory import get_ai_command


REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT = (
    "Respond with the single English word 'ok' (no quotes, no punctuation, "
    "no surrounding text). This is a benchmark probe — keep the answer minimal."
)


@dataclass
class CellResult:
    backend: str
    concurrency: int
    invocations: int
    turns_per_invocation: int
    durations: list[float]
    errors: list[str]

    def percentile(self, p: float) -> float | None:
        if not self.durations:
            return None
        xs = sorted(self.durations)
        import math
        k = max(0, math.ceil(p / 100 * len(xs)) - 1)
        return xs[k]


SUBPROCESS_CMD = "codex exec --sandbox workspace-write --model gpt-5.5 -"


def make_app_server_config() -> dict:
    return {
        "codex_app_server": {
            "enabled": True,
            "command": "codex app-server",
            "transport": "stdio",
            "thread_strategy": "per_session",
            "effort": "medium",
            "model": "gpt-5.5",
            "timeout_seconds": 120,
            "fallback": "subprocess",
        },
        "llm": {"command": SUBPROCESS_CMD},
    }


def make_subprocess_config() -> dict:
    return {"llm": {"command": SUBPROCESS_CMD}}


def run_invocation(backend: str, turns: int, project_root: Path) -> tuple[float, str | None]:
    """One invocation = construct AiCommand, run N turns sequentially, close."""
    config = make_app_server_config() if backend == "app_server" else make_subprocess_config()
    cmd = get_ai_command(config, project_root=project_root)
    start = time.perf_counter()
    err: str | None = None
    try:
        for _ in range(turns):
            cmd.invoke(PROMPT, timeout=120)
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        try:
            cmd.close()
        except Exception:  # noqa: BLE001
            pass
    elapsed = time.perf_counter() - start
    return elapsed, err


def run_cell(backend: str, concurrency: int, invocations: int, turns: int, project_root: Path) -> CellResult:
    durations: list[float] = []
    errors: list[str] = []
    print(f"  → cell backend={backend} C={concurrency} N={invocations} turns={turns} ...", flush=True)
    cell_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(run_invocation, backend, turns, project_root) for _ in range(invocations)]
        for fut in as_completed(futures):
            dur, err = fut.result()
            durations.append(dur)
            if err:
                errors.append(err)
    cell_wall = time.perf_counter() - cell_start
    print(f"    done in {cell_wall:.1f}s, errors={len(errors)}", flush=True)
    return CellResult(
        backend=backend,
        concurrency=concurrency,
        invocations=invocations,
        turns_per_invocation=turns,
        durations=durations,
        errors=errors,
    )


def format_summary(cells: list[CellResult]) -> str:
    lines = []
    lines.append(
        f"{'backend':<12} {'C':>3} {'N':>4} {'turns':>5} "
        f"{'mean':>7} {'P50':>7} {'P90':>7} {'P95':>7} {'P99':>7} {'max':>7} {'err':>3}"
    )
    lines.append("-" * 84)
    for cell in cells:
        durs = cell.durations
        if durs:
            mean = statistics.fmean(durs)
            row = (
                f"{cell.backend:<12} {cell.concurrency:>3} {cell.invocations:>4} "
                f"{cell.turns_per_invocation:>5} "
                f"{mean:>7.2f} {cell.percentile(50):>7.2f} {cell.percentile(90):>7.2f} "
                f"{cell.percentile(95):>7.2f} {cell.percentile(99):>7.2f} {max(durs):>7.2f} "
                f"{len(cell.errors):>3}"
            )
        else:
            row = f"{cell.backend:<12} {cell.concurrency:>3} {cell.invocations:>4} {cell.turns_per_invocation:>5} (no successful runs)"
        lines.append(row)
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["subprocess", "app_server", "both"], default="both")
    parser.add_argument("--concurrency", default="1,10,30", help="comma-separated list")
    parser.add_argument("--invocations", type=int, default=30, help="per cell")
    parser.add_argument("--turns-per-invocation", type=int, default=3)
    parser.add_argument("--project-root", default=str(REPO_ROOT / "bench_fixture"))
    parser.add_argument("--output", default=None, help="JSONL output path")
    parser.add_argument("--warmup", action="store_true", help="run 1 throwaway invocation first")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    project_root.mkdir(parents=True, exist_ok=True)

    concurrencies = [int(s) for s in args.concurrency.split(",") if s.strip()]
    backends = [args.backend] if args.backend != "both" else ["subprocess", "app_server"]

    if args.warmup:
        print("Warming up (1 invocation per backend, ignored from measurement)...", flush=True)
        for backend in backends:
            run_invocation(backend, args.turns_per_invocation, project_root)

    cells: list[CellResult] = []
    for backend in backends:
        for c in concurrencies:
            cells.append(run_cell(backend, c, args.invocations, args.turns_per_invocation, project_root))

    summary = format_summary(cells)
    print("\n" + summary)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w") as fh:
            for cell in cells:
                fh.write(json.dumps({
                    "backend": cell.backend,
                    "concurrency": cell.concurrency,
                    "invocations": cell.invocations,
                    "turns_per_invocation": cell.turns_per_invocation,
                    "durations": cell.durations,
                    "errors": cell.errors,
                }) + "\n")
        print(f"\nJSONL: {out}")


if __name__ == "__main__":
    main()
