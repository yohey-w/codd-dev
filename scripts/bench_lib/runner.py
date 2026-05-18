"""Async benchmark execution for one target/backend/concurrency cell."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Awaitable, Callable, Mapping, Sequence

import click

from .schema import CellResult, RunRecord, utc_now_iso


RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "ratelimit",
    "too many requests",
    "rate_limited",
)


class AbortError(click.ClickException):
    """Raised when a benchmark guardrail rejects the requested cell."""


@dataclass(frozen=True)
class CompletedRun:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    fallback_to_subprocess: bool = False

    @property
    def success(self) -> bool:
        return self.returncode == 0


Executor = Callable[[Sequence[str], Mapping[str, str], Path], Awaitable[CompletedRun]]
Sleep = Callable[[float], Awaitable[None]]


async def run_cell(
    *,
    target: str,
    backend: str,
    concurrency: int,
    rounds: int,
    warmup: int,
    transport: str,
    project_root: Path,
    dry_run: bool = False,
    allow_high_concurrency: bool = False,
    task_id: str | None = None,
    retry_sleep_seconds: float = 30.0,
    max_retries: int = 3,
    executor: Executor | None = None,
    sleep: Sleep = asyncio.sleep,
) -> CellResult:
    """Run one benchmark cell and return raw records."""

    if concurrency >= 100 and not allow_high_concurrency:
        raise AbortError("concurrency 100 requires --allow-high-concurrency")
    if rounds < 1:
        raise AbortError("--rounds must be at least 1")
    if warmup < 0:
        raise AbortError("--warmup must not be negative")

    resolved_executor = _dry_run_executor if dry_run else (executor or _subprocess_executor)
    cell = CellResult(
        target=target,
        backend=backend,
        transport=transport,
        concurrency=concurrency,
        rounds=rounds,
        warmup=warmup,
    )

    for _ in range(warmup):
        await _run_round(
            target=target,
            backend=backend,
            transport=transport,
            concurrency=concurrency,
            iteration=0,
            project_root=project_root,
            task_id=task_id,
            executor=resolved_executor,
            sleep=sleep,
            retry_sleep_seconds=retry_sleep_seconds,
            max_retries=max_retries,
            record=False,
        )

    for iteration in range(1, rounds + 1):
        records = await _run_round(
            target=target,
            backend=backend,
            transport=transport,
            concurrency=concurrency,
            iteration=iteration,
            project_root=project_root,
            task_id=task_id,
            executor=resolved_executor,
            sleep=sleep,
            retry_sleep_seconds=retry_sleep_seconds,
            max_retries=max_retries,
            record=True,
        )
        cell.records.extend(records)
        if any(record.error == "rate_limited" for record in records):
            cell.skipped = True
            cell.skip_reason = "rate limit after retries"
            break
    return cell


async def _run_round(
    *,
    target: str,
    backend: str,
    transport: str,
    concurrency: int,
    iteration: int,
    project_root: Path,
    task_id: str | None,
    executor: Executor,
    sleep: Sleep,
    retry_sleep_seconds: float,
    max_retries: int,
    record: bool,
) -> list[RunRecord]:
    started = time.perf_counter()
    tasks = [
        _run_worker(
            target=target,
            backend=backend,
            transport=transport,
            concurrency=concurrency,
            iteration=iteration,
            worker_index=worker_index,
            project_root=project_root,
            task_id=task_id,
            executor=executor,
            sleep=sleep,
            retry_sleep_seconds=retry_sleep_seconds,
            max_retries=max_retries,
        )
        for worker_index in range(1, concurrency + 1)
    ]
    records = await asyncio.gather(*tasks)
    round_duration = time.perf_counter() - started
    if not record:
        return []
    return [
        RunRecord(
            ts=run.ts,
            target=run.target,
            backend=run.backend,
            transport=run.transport,
            concurrency=run.concurrency,
            iteration=run.iteration,
            duration_seconds=run.duration_seconds,
            success=run.success,
            fallback_to_subprocess=run.fallback_to_subprocess,
            error=run.error,
            stdout_size_bytes=run.stdout_size_bytes,
            worker_index=run.worker_index,
            attempt=run.attempt,
            round_duration_seconds=round_duration,
        )
        for run in records
    ]


async def _run_worker(
    *,
    target: str,
    backend: str,
    transport: str,
    concurrency: int,
    iteration: int,
    worker_index: int,
    project_root: Path,
    task_id: str | None,
    executor: Executor,
    sleep: Sleep,
    retry_sleep_seconds: float,
    max_retries: int,
) -> RunRecord:
    command = command_for_target(target, project_root, task_id=task_id)
    env = env_for_backend(backend, transport)
    for attempt in range(1, max_retries + 1):
        started = time.perf_counter()
        completed = await executor(command, env, project_root)
        duration = time.perf_counter() - started
        combined_output = f"{completed.stdout}\n{completed.stderr}"
        rate_limited = _is_rate_limited(combined_output)
        if completed.success and not rate_limited:
            return _record(
                target=target,
                backend=backend,
                transport=transport,
                concurrency=concurrency,
                iteration=iteration,
                worker_index=worker_index,
                duration=duration,
                success=True,
                fallback_to_subprocess=completed.fallback_to_subprocess or _is_fallback(combined_output),
                error=None,
                stdout=completed.stdout,
                attempt=attempt,
            )
        if rate_limited and attempt < max_retries:
            await sleep(retry_sleep_seconds)
            continue
        return _record(
            target=target,
            backend=backend,
            transport=transport,
            concurrency=concurrency,
            iteration=iteration,
            worker_index=worker_index,
            duration=duration,
            success=False,
            fallback_to_subprocess=completed.fallback_to_subprocess or _is_fallback(combined_output),
            error="rate_limited" if rate_limited else _error_text(completed),
            stdout=completed.stdout,
            attempt=attempt,
        )
    raise AssertionError("unreachable")


def command_for_target(target: str, project_root: Path, *, task_id: str | None = None) -> list[str]:
    if target == "implement":
        command = ["codd", "implement", "run", "--path", str(project_root)]
        if task_id:
            command.extend(["--task", task_id])
        return command
    if target == "verify":
        return [
            "codd",
            "verify",
            "--path",
            str(project_root),
            "--auto-repair",
            "--max-attempts",
            "1",
        ]
    if target == "fix":
        return [
            "codd",
            "fix",
            "Benchmark fixture for Codex App Server transport.",
            "--path",
            str(project_root),
            "--dry-run",
            "--non-interactive",
            "--no-push",
        ]
    raise AbortError(f"unsupported target: {target}")


def env_for_backend(backend: str, transport: str) -> dict[str, str]:
    env = {
        **os.environ,
        "CODD_BENCH_BACKEND": backend,
        "CODD_BENCH_TRANSPORT": transport,
    }
    if backend == "app_server":
        env["CODD_CODEX_APP_SERVER_ENABLED"] = "1"
        env["CODD_CODEX_APP_SERVER_TRANSPORT"] = transport
    if backend == "subprocess":
        env["CODD_CODEX_APP_SERVER_ENABLED"] = "0"
    return env


async def _subprocess_executor(command: Sequence[str], env: Mapping[str, str], cwd: Path) -> CompletedRun:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        env=dict(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return CompletedRun(process.returncode or 0, stdout=stdout, stderr=stderr)


async def _dry_run_executor(command: Sequence[str], env: Mapping[str, str], cwd: Path) -> CompletedRun:
    await asyncio.sleep(0)
    backend = env.get("CODD_BENCH_BACKEND", "unknown")
    return CompletedRun(
        0,
        stdout=f"dry-run backend={backend} cwd={cwd} command={' '.join(command)}\n",
        stderr="",
        fallback_to_subprocess=False,
    )


def _record(
    *,
    target: str,
    backend: str,
    transport: str,
    concurrency: int,
    iteration: int,
    worker_index: int,
    duration: float,
    success: bool,
    fallback_to_subprocess: bool,
    error: str | None,
    stdout: str,
    attempt: int,
) -> RunRecord:
    return RunRecord(
        ts=utc_now_iso(),
        target=target,
        backend=backend,
        transport=transport,
        concurrency=concurrency,
        iteration=iteration,
        worker_index=worker_index,
        duration_seconds=duration,
        success=success,
        fallback_to_subprocess=fallback_to_subprocess,
        error=error,
        stdout_size_bytes=len(stdout.encode("utf-8")),
        attempt=attempt,
    )


def _is_rate_limited(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def _is_fallback(output: str) -> bool:
    lowered = output.lower()
    return "fallback_to_subprocess" in lowered or "app server fallback" in lowered


def _error_text(completed: CompletedRun) -> str:
    detail = completed.stderr.strip() or completed.stdout.strip()
    if not detail:
        return f"exit_{completed.returncode}"
    return detail[:500]
