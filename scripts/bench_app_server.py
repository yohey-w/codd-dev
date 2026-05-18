#!/usr/bin/env python3
"""Benchmark CoDD's Codex App Server transport against subprocess AI calls."""

from __future__ import annotations

import asyncio
from datetime import datetime
import platform
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

import click

try:
    from .bench_lib.reporter import write_jsonl, write_markdown
    from .bench_lib.runner import AbortError, run_cell
    from .bench_lib.schema import BenchResult, utc_now_iso
except ImportError:  # pragma: no cover - direct script execution path
    from bench_lib.reporter import write_jsonl, write_markdown
    from bench_lib.runner import AbortError, run_cell
    from bench_lib.schema import BenchResult, utc_now_iso


TARGETS = ("implement", "verify", "fix")
BACKENDS = ("subprocess", "app_server")


@click.command()
@click.option("--target", type=click.Choice(["implement", "verify", "fix", "all"]), default="all", show_default=True)
@click.option("--backend", type=click.Choice(["subprocess", "app_server", "both"]), default="both", show_default=True)
@click.option("--concurrency", callback=lambda _, __, value: parse_concurrency(value), default="1,10,50", show_default=True)
@click.option("--rounds", default=5, type=click.IntRange(min=1), show_default=True)
@click.option("--repeats", default=None, type=click.IntRange(min=1), help="Alias for --rounds.")
@click.option("--warmup", default=1, type=click.IntRange(min=0), show_default=True)
@click.option("--transport", type=click.Choice(["stdio", "unix", "auto"]), default="auto", show_default=True)
@click.option("--project-root", type=click.Path(file_okay=False, path_type=Path), default=Path("."), show_default=True)
@click.option("--output-dir", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--dry-run", is_flag=True, help="Exercise script flow without invoking real AI-backed CoDD commands.")
@click.option("--allow-high-concurrency", is_flag=True, help="Required when any requested concurrency is 100 or higher.")
@click.option("--task-id", default=None, help="Optional implementation task id for implement target.")
@click.option("--retry-sleep", default=30.0, type=click.FloatRange(min=0), show_default=True)
def main(
    target: str,
    backend: str,
    concurrency: tuple[int, ...],
    rounds: int,
    repeats: int | None,
    warmup: int,
    transport: str,
    project_root: Path,
    output_dir: Path | None,
    dry_run: bool,
    allow_high_concurrency: bool,
    task_id: str | None,
    retry_sleep: float,
) -> None:
    """Run a benchmark matrix and write raw JSONL plus Markdown summary."""

    project_root = project_root.resolve()
    output_dir = (output_dir or project_root / ".codd" / "bench").resolve()
    run_rounds = repeats if repeats is not None else rounds
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result = BenchResult(
        created_at=utc_now_iso(),
        project_root=project_root,
        output_dir=output_dir,
        raw_jsonl=output_dir / f"raw_{timestamp}.jsonl",
        summary_markdown=output_dir / f"summary_{timestamp}.md",
        environment=collect_environment(project_root, include_external=not dry_run),
    )

    for cell_target, cell_backend, cell_concurrency, cell_transport in build_matrix(
        target=target,
        backend=backend,
        concurrency=concurrency,
        transport=transport,
    ):
        cell = asyncio.run(
            run_cell(
                target=cell_target,
                backend=cell_backend,
                concurrency=cell_concurrency,
                rounds=run_rounds,
                warmup=warmup,
                transport=cell_transport,
                project_root=project_root,
                dry_run=dry_run,
                allow_high_concurrency=allow_high_concurrency,
                task_id=task_id,
                retry_sleep_seconds=retry_sleep,
            )
        )
        result.cells.append(cell)
        click.echo(
            f"{cell_target}/{cell_backend}/concurrency={cell_concurrency}: "
            f"records={len(cell.records)} skipped={cell.skipped}"
        )

    write_jsonl(result)
    write_markdown(result)
    click.echo(f"Raw JSONL: {result.raw_jsonl}")
    click.echo(f"Summary: {result.summary_markdown}")


def parse_concurrency(value: str | int | Iterable[int]) -> tuple[int, ...]:
    if isinstance(value, int):
        values = (value,)
    elif isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts:
            raise click.BadParameter("must include at least one integer")
        try:
            values = tuple(int(part) for part in parts)
        except ValueError as exc:
            raise click.BadParameter("must be a comma-separated list of integers") from exc
    else:
        values = tuple(int(item) for item in value)
    if any(item < 1 for item in values):
        raise click.BadParameter("must be >= 1")
    return tuple(dict.fromkeys(values))


def build_matrix(
    *,
    target: str,
    backend: str,
    concurrency: tuple[int, ...],
    transport: str,
) -> list[tuple[str, str, int, str]]:
    targets = TARGETS if target == "all" else (target,)
    backends = BACKENDS if backend == "both" else (backend,)
    matrix: list[tuple[str, str, int, str]] = []
    for cell_target in targets:
        for cell_backend in backends:
            for cell_concurrency in concurrency:
                matrix.append(
                    (
                        cell_target,
                        cell_backend,
                        cell_concurrency,
                        resolve_transport(cell_backend, cell_concurrency, transport),
                    )
                )
    return matrix


def resolve_transport(backend: str, concurrency: int, transport: str) -> str:
    if backend == "subprocess":
        return "subprocess"
    if transport != "auto":
        return transport
    return "stdio" if concurrency < 10 else "unix"


def collect_environment(project_root: Path, *, include_external: bool = True) -> dict[str, str]:
    external = {
        "codd_version": "",
        "codex_version": "",
        "git_head": "",
        "codex_binary": shutil.which("codex") or "",
        "codd_binary": shutil.which("codd") or "",
    }
    if include_external:
        external.update(
            {
                "codd_version": _command_version(["codd", "--version"]),
                "codex_version": _command_version(["codex", "--version"]),
                "git_head": _command_version(["git", "rev-parse", "HEAD"], cwd=project_root),
            }
        )
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        **external,
    }


def _command_version(command: list[str], cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout.strip() or completed.stderr.strip()).splitlines()[0] if (
        completed.stdout.strip() or completed.stderr.strip()
    ) else ""


if __name__ == "__main__":
    main()
