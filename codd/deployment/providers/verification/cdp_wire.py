"""Minimal CDP wire client built on websocket JSON-RPC frames."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Mapping
from typing import Any


class CdpWireError(RuntimeError):
    """Base error raised by the CDP wire client."""


class CdpWireConnectionError(CdpWireError):
    """Raised when a CDP websocket cannot be opened."""


class CdpWireProtocolError(CdpWireError):
    """Raised when the CDP peer returns an error or invalid response."""


class CdpWireTimeout(CdpWireError):
    """Raised when no matching response arrives before timeout."""


SocketFactory = Callable[[str, float | None], Any]


class CdpWire:
    """Send commands over a CDP websocket using JSON-RPC request ids."""

    def __init__(self, socket_factory: SocketFactory | None = None) -> None:
        self._socket_factory = socket_factory or _default_socket_factory
        self._socket: Any | None = None
        self._next_id = 1

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self, endpoint: str, timeout: float | None = None) -> None:
        """Open a websocket connection to ``endpoint``."""
        try:
            self._socket = self._socket_factory(endpoint, timeout)
        except Exception as exc:  # pragma: no cover - exact websocket errors vary
            if _is_timeout_error(exc):
                raise CdpWireTimeout(f"CDP connect timeout: {endpoint}") from exc
            raise CdpWireConnectionError(f"CDP connect failed: {endpoint}") from exc

    def close(self) -> None:
        """Close the websocket connection if it is open."""
        if self._socket is None:
            return
        try:
            self._socket.close()
        finally:
            self._socket = None

    def send_command(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send one CDP command and return its ``result`` payload."""
        if self._socket is None:
            raise CdpWireConnectionError("CDP websocket is not connected")

        request_id = self._next_id
        self._next_id += 1
        frame = {"id": request_id, "method": method, "params": dict(params or {})}

        if timeout is not None and hasattr(self._socket, "settimeout"):
            self._socket.settimeout(timeout)

        try:
            self._socket.send(json.dumps(frame, sort_keys=True))
            while True:
                response = self._recv_json()
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise CdpWireProtocolError(f"CDP command failed: {response['error']}")
                result = response.get("result", {})
                if not isinstance(result, dict):
                    raise CdpWireProtocolError("CDP response result must be an object")
                return result
        except CdpWireError:
            raise
        except Exception as exc:  # pragma: no cover - exact websocket errors vary
            if _is_timeout_error(exc):
                raise CdpWireTimeout(f"CDP command timeout: {method}") from exc
            raise CdpWireProtocolError(f"CDP command failed: {method}") from exc

    def _recv_json(self) -> dict[str, Any]:
        if self._socket is None:
            raise CdpWireConnectionError("CDP websocket is not connected")
        try:
            payload = self._socket.recv()
        except Exception as exc:  # pragma: no cover - exact websocket errors vary
            if _is_timeout_error(exc):
                raise CdpWireTimeout("CDP response timeout") from exc
            raise
        try:
            data = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CdpWireProtocolError("CDP response was not valid JSON") from exc
        if not isinstance(data, dict):
            raise CdpWireProtocolError("CDP response must be an object")
        return data


def _default_socket_factory(endpoint: str, timeout: float | None) -> Any:
    import websocket

    return websocket.create_connection(endpoint, timeout=timeout)


def _is_timeout_error(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout)) or "Timeout" in type(exc).__name__


__all__ = [
    "CdpWire",
    "CdpWireConnectionError",
    "CdpWireError",
    "CdpWireProtocolError",
    "CdpWireTimeout",
]
