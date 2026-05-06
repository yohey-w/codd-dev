"""Chunked task step runner with durable history."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Literal, Mapping

import yaml


RunStatus = Literal["SUCCESS", "PARTIAL", "TIMEOUT", "USER_INTERRUPTED"]


@dataclass
class ChunkedExecution:
    chunk_index: int
    step_ids: list[str]
    ai_command_output: str
    duration_seconds: float
    exit_code: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_index": self.chunk_index,
            "step_ids": list(self.step_ids),
            "ai_command_output": self.ai_command_output,
            "duration_seconds": self.duration_seconds,
            "exit_code": self.exit_code,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChunkedExecution":
        return cls(
            chunk_index=int(payload["chunk_index"]),
            step_ids=[str(item) for item in payload.get("step_ids", [])],
            ai_command_output=str(payload.get("ai_command_output", "")),
            duration_seconds=float(payload.get("duration_seconds", 0.0)),
            exit_code=int(payload.get("exit_code", 0)),
        )


@dataclass
class ChunkedRunResult:
    completed_chunks: list[ChunkedExecution]
    total_chunks: int
    status: RunStatus
    history_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_chunks": [chunk.to_dict() for chunk in self.completed_chunks],
            "total_chunks": self.total_chunks,
            "status": self.status,
            "history_path": self.history_path.as_posix(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ChunkedRunResult":
        return cls(
            completed_chunks=[
                ChunkedExecution.from_dict(item)
                for item in payload.get("completed_chunks", [])
                if isinstance(item, Mapping)
            ],
            total_chunks=int(payload["total_chunks"]),
            status=str(payload["status"]),  # type: ignore[arg-type]
            history_path=Path(str(payload["history_path"])),
        )


class ChunkedRunner:
    def __init__(
        self,
        chunk_size: int = 5,
        timeout_per_chunk: int = 600,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1")
        if timeout_per_chunk < 1:
            raise ValueError("timeout_per_chunk must be at least 1")
        self.chunk_size = chunk_size
        self.timeout_per_chunk = timeout_per_chunk
        self.progress_callback = progress_callback

    def run_steps(
        self,
        task: Any,
        impl_steps: list[Any],
        ai_command: str,
        project_root: Path,
    ) -> ChunkedRunResult:
        history_path = _new_history_path(project_root)
        return self._run_steps(
            task=task,
            impl_steps=impl_steps,
            ai_command=ai_command,
            project_root=project_root,
            history_path=history_path,
            resume=False,
        )

    def resume_steps(
        self,
        task: Any,
        impl_steps: list[Any],
        ai_command: str,
        project_root: Path,
        history: str | Path,
    ) -> ChunkedRunResult:
        return self._run_steps(
            task=task,
            impl_steps=impl_steps,
            ai_command=ai_command,
            project_root=project_root,
            history_path=_resolve_history_path(project_root, history),
            resume=True,
        )

    def _run_steps(
        self,
        *,
        task: Any,
        impl_steps: list[Any],
        ai_command: str,
        project_root: Path,
        history_path: Path,
        resume: bool,
    ) -> ChunkedRunResult:
        project_root = project_root.resolve()
        chunks = _split_steps(impl_steps, self.chunk_size)
        total_chunks = len(chunks)
        history_path.mkdir(parents=True, exist_ok=True)
        (history_path / "chunks").mkdir(parents=True, exist_ok=True)
        _write_yaml(history_path / "task.yaml", _task_payload(task))

        completed = _read_completed_chunks(history_path) if resume else []
        completed_by_index = {chunk.chunk_index: chunk for chunk in completed}
        try:
            for chunk_index, chunk in enumerate(chunks):
                if chunk_index in completed_by_index:
                    continue
                prompt = _render_chunk_prompt(
                    task=task,
                    chunk=chunk,
                    completed_chunks=completed,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                )
                started = time.monotonic()
                try:
                    output, exit_code = self._invoke(ai_command, prompt, project_root)
                except _ChunkTimeout as exc:
                    timed_out = ChunkedExecution(
                        chunk_index=chunk_index,
                        step_ids=_step_ids(chunk),
                        ai_command_output=exc.output,
                        duration_seconds=time.monotonic() - started,
                        exit_code=-1,
                    )
                    _write_chunk(history_path, timed_out, timed_out=True)
                    return self._finish(history_path, completed, total_chunks, "TIMEOUT")

                execution = ChunkedExecution(
                    chunk_index=chunk_index,
                    step_ids=_step_ids(chunk),
                    ai_command_output=output,
                    duration_seconds=time.monotonic() - started,
                    exit_code=exit_code,
                )
                completed.append(execution)
                _write_chunk(history_path, execution)
                if self.progress_callback is not None:
                    self.progress_callback(len(completed), total_chunks)
                if exit_code != 0:
                    return self._finish(history_path, completed, total_chunks, "PARTIAL")
        except KeyboardInterrupt:
            return self._finish(history_path, completed, total_chunks, "USER_INTERRUPTED")

        return self._finish(history_path, completed, total_chunks, "SUCCESS")

    def _invoke(self, ai_command: str, prompt: str, project_root: Path) -> tuple[str, int]:
        command = shlex.split(ai_command)
        if not command:
            raise ValueError("ai_command must not be empty")

        try:
            process = subprocess.Popen(
                command,
                cwd=str(project_root),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ValueError(f"ai_command not found: {command[0]}") from exc

        if _can_stream(process):
            return self._invoke_with_streams(process, prompt)

        try:
            stdout, stderr = process.communicate(prompt, timeout=self.timeout_per_chunk)
        except subprocess.TimeoutExpired as exc:
            _stop_process(process)
            stdout, stderr = process.communicate()
            raise _ChunkTimeout(_joined_output(stdout, stderr, exc)) from exc
        except KeyboardInterrupt:
            _stop_process(process)
            _wait_quietly(process)
            raise

        _tee(stdout, stderr)
        return _joined_output(stdout, stderr), int(process.returncode or 0)

    def _invoke_with_streams(self, process: subprocess.Popen, prompt: str) -> tuple[str, int]:
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        threads = [
            threading.Thread(target=_read_pipe, args=(process.stdout, stdout_parts, sys.stdout), daemon=True),
            threading.Thread(target=_read_pipe, args=(process.stderr, stderr_parts, sys.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()

        try:
            if process.stdin is not None:
                try:
                    process.stdin.write(prompt)
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            exit_code = process.wait(timeout=self.timeout_per_chunk)
        except subprocess.TimeoutExpired as exc:
            _stop_process(process)
            _wait_quietly(process)
            _join_threads(threads)
            raise _ChunkTimeout(_joined_output("".join(stdout_parts), "".join(stderr_parts), exc)) from exc
        except KeyboardInterrupt:
            _stop_process(process)
            _wait_quietly(process)
            _join_threads(threads)
            raise

        _join_threads(threads)
        return _joined_output("".join(stdout_parts), "".join(stderr_parts)), int(exit_code or 0)

    def _finish(
        self,
        history_path: Path,
        completed_chunks: list[ChunkedExecution],
        total_chunks: int,
        status: RunStatus,
    ) -> ChunkedRunResult:
        result = ChunkedRunResult(
            completed_chunks=list(completed_chunks),
            total_chunks=total_chunks,
            status=status,
            history_path=history_path,
        )
        _write_yaml(history_path / "final_status.yaml", result.to_dict())
        return result


class _ChunkTimeout(Exception):
    def __init__(self, output: str) -> None:
        super().__init__("chunk timed out")
        self.output = output


def _split_steps(steps: list[Any], chunk_size: int) -> list[list[Any]]:
    return [steps[index:index + chunk_size] for index in range(0, len(steps), chunk_size)]


def _render_chunk_prompt(
    *,
    task: Any,
    chunk: list[Any],
    completed_chunks: list[ChunkedExecution],
    chunk_index: int,
    total_chunks: int,
) -> str:
    payload = {
        "task": _task_payload(task),
        "chunk": {
            "index": chunk_index,
            "count": total_chunks,
            "steps": [_step_payload(step) for step in chunk],
        },
        "completed_chunks": [completed.to_dict() for completed in completed_chunks],
        "instructions": [
            "Apply only the current chunk.",
            "Keep prior chunk work intact.",
            "Return a concise result summary.",
        ],
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _task_payload(task: Any) -> dict[str, Any]:
    if is_dataclass(task):
        return asdict(task)
    if isinstance(task, Mapping):
        return dict(task)
    payload: dict[str, Any] = {}
    for name in (
        "task_id",
        "id",
        "title",
        "summary",
        "module_hint",
        "deliverable",
        "output_dir",
        "task_context",
    ):
        value = getattr(task, name, None)
        if value is not None:
            payload[name] = value
    return payload or {"value": str(task)}


def _step_payload(step: Any) -> dict[str, Any]:
    if hasattr(step, "to_dict"):
        value = step.to_dict()
        return dict(value) if isinstance(value, Mapping) else {"value": value}
    if is_dataclass(step):
        return asdict(step)
    if isinstance(step, Mapping):
        return dict(step)
    payload: dict[str, Any] = {}
    for name in (
        "id",
        "kind",
        "rationale",
        "source_design_section",
        "target_path_hint",
        "expected_outputs",
    ):
        value = getattr(step, name, None)
        if value is not None:
            payload[name] = value
    return payload or {"value": str(step)}


def _step_ids(steps: list[Any]) -> list[str]:
    ids: list[str] = []
    for index, step in enumerate(steps):
        if isinstance(step, Mapping):
            value = step.get("id")
        else:
            value = getattr(step, "id", None)
        ids.append(str(value or f"step_{index + 1}"))
    return ids


def _new_history_path(project_root: Path) -> Path:
    root = project_root / ".codd" / "chunked_run_history"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    candidate = root / stamp
    counter = 1
    while candidate.exists():
        candidate = root / f"{stamp}-{counter}"
        counter += 1
    return candidate


def _resolve_history_path(project_root: Path, history: str | Path) -> Path:
    path = Path(history)
    if path.is_absolute():
        return path
    if len(path.parts) > 1:
        return (project_root / path).resolve()
    return (project_root / ".codd" / "chunked_run_history" / path).resolve()


def _write_chunk(history_path: Path, execution: ChunkedExecution, *, timed_out: bool = False) -> None:
    payload = execution.to_dict()
    if timed_out:
        payload["timed_out"] = True
    _write_yaml(history_path / "chunks" / f"chunk_{execution.chunk_index}.yaml", payload)


def _read_completed_chunks(history_path: Path) -> list[ChunkedExecution]:
    chunk_dir = history_path / "chunks"
    if not chunk_dir.is_dir():
        return []
    completed: list[ChunkedExecution] = []
    for path in sorted(chunk_dir.glob("chunk_*.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(payload, Mapping) and not payload.get("timed_out"):
            completed.append(ChunkedExecution.from_dict(payload))
    completed.sort(key=lambda chunk: chunk.chunk_index)
    return completed


def _write_yaml(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(dict(payload), sort_keys=False, allow_unicode=True), encoding="utf-8")


def _stop_process(process: subprocess.Popen) -> None:
    pid = getattr(process, "pid", None)
    if pid is not None:
        try:
            os.killpg(pid, signal.SIGTERM)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    process.terminate()


def _can_stream(process: subprocess.Popen) -> bool:
    return (
        getattr(process, "stdin", None) is not None
        and getattr(process, "stdout", None) is not None
        and getattr(process, "stderr", None) is not None
        and hasattr(process, "wait")
    )


def _read_pipe(pipe: Any, parts: list[str], target: Any) -> None:
    if pipe is None:
        return
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            parts.append(line)
            print(line, end="", file=target)
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _join_threads(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=1)


def _wait_quietly(process: subprocess.Popen) -> None:
    if not hasattr(process, "wait"):
        return
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _joined_output(stdout: str | None, stderr: str | None, exc: subprocess.TimeoutExpired | None = None) -> str:
    parts = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    if exc is not None:
        if exc.output:
            parts.append(_bytes_to_text(exc.output))
        if exc.stderr:
            parts.append(_bytes_to_text(exc.stderr))
    return "".join(parts)


def _bytes_to_text(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _tee(stdout: str | None, stderr: str | None) -> None:
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)


__all__ = [
    "ChunkedExecution",
    "ChunkedRunner",
    "ChunkedRunResult",
]
