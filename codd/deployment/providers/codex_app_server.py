"""Codex App Server JSON-RPC client for AI command adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import shlex
from typing import Any, Mapping, Sequence

from codd.deployment.providers.ai_command import AiCommandError


DEFAULT_APP_SERVER_COMMAND = "codex app-server --listen stdio://"
_JSONRPC_VERSION = "2.0"
_TURN_COMPLETED = object()


class CodexAppServerInitError(AiCommandError):
    """Raised when the Codex App Server transport cannot be initialized."""


class CodexAppServerTurnError(AiCommandError):
    """Raised when a Codex App Server turn fails."""


class CodexAppServerTimeout(AiCommandError):
    """Raised when a Codex App Server turn exceeds its timeout."""


@dataclass
class CodexAppServerClient:
    """Small asyncio stdio client for the Codex App Server protocol."""

    command: str | Sequence[str] = DEFAULT_APP_SERVER_COMMAND
    transport: str = "stdio"
    cwd: Path | str | None = None
    framing: str = "json_lines"
    _process: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _request_id: int = field(default=0, init=False, repr=False)
    _initialized: bool = field(default=False, init=False, repr=False)
    _pending_notifications: list[Mapping[str, Any]] = field(default_factory=list, init=False, repr=False)

    async def start_thread(
        self,
        model: str | None,
        effort: str | None,
        cwd: Path | str | None,
        base_instructions: str | None,
    ) -> str:
        """Start a Codex App Server thread and return its thread id."""

        await self._connect()
        params = _compact(
            {
                "baseInstructions": base_instructions,
                "cwd": str(cwd) if cwd is not None else None,
                "model": model,
            }
        )
        result = await self._request("thread/start", params, CodexAppServerInitError)
        thread_id = _thread_id(result)
        if not thread_id:
            raise CodexAppServerInitError("thread/start response did not include a thread id")
        return thread_id

    async def send_turn(
        self,
        thread_id: str,
        input: str,
        timeout: float | None,
        *,
        model: str | None = None,
        effort: str | None = None,
        cwd: Path | str | None = None,
    ) -> str:
        """Send one user turn and return the final streamed agent message."""

        try:
            return await asyncio.wait_for(
                self._send_turn(thread_id, input, model=model, effort=effort, cwd=cwd),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise CodexAppServerTimeout(f"Codex App Server turn timed out after {timeout:g}s") from exc

    async def archive_thread(self, thread_id: str) -> None:
        """Archive a thread if the server accepts the request."""

        await self._connect()
        await self._request("thread/archive", {"threadId": thread_id}, CodexAppServerTurnError)

    async def close(self) -> None:
        """Close the stdio child process."""

        process = self._process
        self._process = None
        self._initialized = False
        self._pending_notifications.clear()
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def _send_turn(
        self,
        thread_id: str,
        input_text: str,
        *,
        model: str | None,
        effort: str | None,
        cwd: Path | str | None,
    ) -> str:
        await self._connect()
        params = _compact(
            {
                "threadId": thread_id,
                "cwd": str(cwd) if cwd is not None else None,
                "effort": effort,
                "model": model,
                "input": [{"type": "text", "text": input_text}],
            }
        )
        await self._request("turn/start", params, CodexAppServerTurnError)
        deltas: list[str] = []
        while True:
            while self._pending_notifications:
                result = self._handle_turn_notification(
                    self._pending_notifications.pop(0),
                    thread_id=thread_id,
                    deltas=deltas,
                )
                if result is _TURN_COMPLETED:
                    return "".join(deltas)
            message = await self._read_message()
            result = self._handle_turn_notification(message, thread_id=thread_id, deltas=deltas)
            if result is _TURN_COMPLETED:
                return "".join(deltas)

    async def _connect(self) -> None:
        if self.transport != "stdio":
            raise CodexAppServerInitError(f"unsupported Codex App Server transport: {self.transport}")
        if self._process is None or self._process.returncode is not None:
            command = self._command_parts()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.cwd) if self.cwd is not None else None,
                )
            except OSError as exc:
                raise CodexAppServerInitError(f"failed to start Codex App Server: {exc}") from exc
            self._initialized = False
        if not self._initialized:
            await self._initialize()

    async def _initialize(self) -> None:
        result = await self._request(
            "initialize",
            {
                "clientInfo": {"name": "codd", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
            CodexAppServerInitError,
            connect=False,
        )
        if not isinstance(result, Mapping):
            raise CodexAppServerInitError("initialize response was not an object")
        await self._notify("initialized", {})
        self._initialized = True

    async def _request(
        self,
        method: str,
        params: Mapping[str, Any],
        error_type: type[AiCommandError],
        *,
        connect: bool = True,
    ) -> Any:
        if connect:
            await self._connect()
        request_id = self._next_request_id()
        await self._write_message(
            {
                "jsonrpc": _JSONRPC_VERSION,
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
        )
        while True:
            message = await self._read_message()
            if message.get("id") != request_id:
                self._pending_notifications.append(message)
                continue
            if "error" in message:
                raise error_type(_jsonrpc_error_message(message["error"]))
            return message.get("result")

    async def _notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": _JSONRPC_VERSION, "method": method}
        if params:
            payload["params"] = dict(params)
        await self._write_message(payload)

    async def _write_message(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerInitError("Codex App Server process is not running")
        encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.framing == "content_length":
            frame = f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii") + encoded
        else:
            frame = encoded + b"\n"
        process.stdin.write(frame)
        await process.stdin.drain()

    async def _read_message(self) -> Mapping[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise CodexAppServerInitError("Codex App Server process is not running")
        while True:
            line = await process.stdout.readline()
            if not line:
                detail = await self._stderr_tail(process)
                raise CodexAppServerInitError(f"Codex App Server closed stdout{detail}")
            if not line.strip():
                continue
            if line.lower().startswith(b"content-length:"):
                return await self._read_content_length_message(line, process.stdout)
            try:
                payload = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise CodexAppServerTurnError(f"invalid JSON-RPC frame: {exc}") from exc
            if not isinstance(payload, Mapping):
                raise CodexAppServerTurnError("JSON-RPC frame must be an object")
            return payload

    async def _read_content_length_message(
        self,
        first_header: bytes,
        stdout: asyncio.StreamReader,
    ) -> Mapping[str, Any]:
        headers = [first_header]
        while True:
            line = await stdout.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            headers.append(line)
        length: int | None = None
        for header in headers:
            name, _, value = header.decode("ascii", errors="replace").partition(":")
            if name.lower() == "content-length":
                length = int(value.strip())
                break
        if length is None:
            raise CodexAppServerTurnError("Content-Length frame did not include a length")
        body = await stdout.readexactly(length)
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise CodexAppServerTurnError("JSON-RPC frame must be an object")
        return payload

    async def _stderr_tail(self, process: asyncio.subprocess.Process) -> str:
        if process.stderr is None or process.returncode is None:
            return ""
        stderr = await process.stderr.read()
        text = stderr.decode("utf-8", errors="replace").strip()
        return f": {text}" if text else ""

    def _handle_turn_notification(
        self,
        message: Mapping[str, Any],
        *,
        thread_id: str,
        deltas: list[str],
    ) -> object | None:
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, Mapping):
            if "error" in message:
                raise CodexAppServerTurnError(_jsonrpc_error_message(message["error"]))
            return None
        if method in {"item/agentMessage/delta", "AgentMessageDeltaNotification"}:
            if params.get("threadId") in (None, thread_id):
                delta = params.get("delta")
                if isinstance(delta, str):
                    deltas.append(delta)
            return None
        if method in {"turn/completed", "TurnCompletedNotification"}:
            if params.get("threadId") not in (None, thread_id):
                return None
            turn = params.get("turn")
            if isinstance(turn, Mapping) and turn.get("status") == "failed":
                raise CodexAppServerTurnError(_turn_error_message(turn))
            if not deltas and isinstance(turn, Mapping):
                fallback_text = _extract_agent_text(turn)
                if fallback_text:
                    deltas.append(fallback_text)
            return _TURN_COMPLETED
        if method in {"turn/failed", "TurnFailedNotification", "error", "ErrorNotification"}:
            raise CodexAppServerTurnError(_notification_error_message(params))
        return None

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _command_parts(self) -> list[str]:
        if isinstance(self.command, str):
            parts = shlex.split(self.command)
        else:
            parts = [str(part) for part in self.command]
        if not parts:
            raise CodexAppServerInitError("Codex App Server command must not be empty")
        return parts


def _compact(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _thread_id(result: Any) -> str | None:
    if not isinstance(result, Mapping):
        return None
    for key in ("threadId", "thread_id"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    thread = result.get("thread")
    if isinstance(thread, Mapping):
        value = thread.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def _jsonrpc_error_message(error: Any) -> str:
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    return str(error)


def _turn_error_message(turn: Mapping[str, Any]) -> str:
    error = turn.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            details = error.get("additionalDetails")
            if isinstance(details, str) and details:
                return f"{message}: {details}"
            return message
    return "Codex App Server turn failed"


def _notification_error_message(params: Mapping[str, Any]) -> str:
    for key in ("message", "error"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, Mapping):
            message = value.get("message")
            if isinstance(message, str) and message:
                return message
    return "Codex App Server turn failed"


def _extract_agent_text(value: Any) -> str:
    texts: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            item_type = str(item.get("type") or "")
            if item_type in {"agent_message", "assistant_message", "message"}:
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    texts.append(text)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return texts[-1] if texts else ""


__all__ = [
    "CodexAppServerClient",
    "CodexAppServerInitError",
    "CodexAppServerTimeout",
    "CodexAppServerTurnError",
    "DEFAULT_APP_SERVER_COMMAND",
]
