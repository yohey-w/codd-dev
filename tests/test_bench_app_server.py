from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner
import pytest

from scripts import bench_app_server
from scripts.bench_lib.aggregator import aggregate_cell
from scripts.bench_lib.runner import AbortError, CompletedRun, run_cell
from scripts.bench_lib.schema import CellResult, RunRecord


def test_dry_run_exits_zero_without_ai_calls(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        bench_app_server.main,
        [
            "--dry-run",
            "--target",
            "implement",
            "--backend",
            "subprocess",
            "--concurrency",
            "1",
            "--rounds",
            "1",
            "--warmup",
            "0",
            "--project-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "bench"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Raw JSONL:" in result.output
    assert list((tmp_path / "bench").glob("raw_*.jsonl"))


def test_mock_runner_five_rounds_calculates_percentiles(tmp_path: Path) -> None:
    durations = iter([0.1, 0.2, 0.3, 0.4, 0.5])

    async def executor(command, env, cwd):
        await asyncio.sleep(next(durations))
        return CompletedRun(0, stdout="ok")

    cell = asyncio.run(
        run_cell(
            target="implement",
            backend="subprocess",
            concurrency=1,
            rounds=5,
            warmup=0,
            transport="subprocess",
            project_root=tmp_path,
            executor=executor,
        )
    )

    summary = aggregate_cell(cell)
    assert summary["records"] == 5
    assert summary["p50"] == pytest.approx(0.3, abs=0.05)
    assert summary["p95"] == pytest.approx(0.5, abs=0.05)


def test_fallback_records_are_reflected_in_fallback_rate() -> None:
    cell = CellResult(target="implement", backend="app_server", transport="stdio", concurrency=1, rounds=2, warmup=0)
    cell.records = [
        RunRecord("ts", "implement", "app_server", "stdio", 1, 1, 1.0, True, False),
        RunRecord("ts", "implement", "app_server", "stdio", 1, 2, 2.0, True, True),
    ]

    summary = aggregate_cell(cell)
    assert summary["fallback_count"] == 1
    assert summary["fallback_rate"] == 0.5


def test_concurrency_100_without_allow_high_concurrency_raises_abort_error(tmp_path: Path) -> None:
    with pytest.raises(AbortError):
        asyncio.run(
            run_cell(
                target="implement",
                backend="subprocess",
                concurrency=100,
                rounds=1,
                warmup=0,
                transport="subprocess",
                project_root=tmp_path,
            )
        )


def test_concurrency_100_with_allow_high_concurrency_runs_mock(tmp_path: Path) -> None:
    cell = asyncio.run(
        run_cell(
            target="implement",
            backend="subprocess",
            concurrency=100,
            rounds=1,
            warmup=0,
            transport="subprocess",
            project_root=tmp_path,
            dry_run=True,
            allow_high_concurrency=True,
        )
    )

    assert len(cell.records) == 100
    assert all(record.success for record in cell.records)


def test_rate_limit_retries_sleep_then_succeeds(tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []

    async def executor(command, env, cwd):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return CompletedRun(1, stderr="HTTP 429 rate limit")
        return CompletedRun(0, stdout="ok")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    cell = asyncio.run(
        run_cell(
            target="implement",
            backend="subprocess",
            concurrency=1,
            rounds=1,
            warmup=0,
            transport="subprocess",
            project_root=tmp_path,
            executor=executor,
            sleep=fake_sleep,
        )
    )

    assert attempts == 3
    assert sleeps == [30.0, 30.0]
    assert cell.records[0].success is True


def test_output_jsonl_and_markdown_are_generated(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        bench_app_server.main,
        [
            "--dry-run",
            "--target",
            "verify",
            "--backend",
            "app_server",
            "--concurrency",
            "1",
            "--rounds",
            "1",
            "--warmup",
            "0",
            "--project-root",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "bench"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert list((tmp_path / "bench").glob("raw_*.jsonl"))
    assert list((tmp_path / "bench").glob("summary_*.md"))


def test_backend_subprocess_only_skips_app_server_cells() -> None:
    matrix = bench_app_server.build_matrix(
        target="all",
        backend="subprocess",
        concurrency=(1,),
        transport="auto",
    )

    assert {backend for _, backend, _, _ in matrix} == {"subprocess"}
    assert len(matrix) == 3
