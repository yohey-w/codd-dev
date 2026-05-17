from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pytest

from codd.deployment.providers.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerTurnError,
)


MOCK_SERVER = r"""
from __future__ import annotations

import json
import sys

fail_turn = "--fail-turn" in sys.argv


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        send({
            "id": request_id,
            "result": {
                "codexHome": "/tmp/codex",
                "platformFamily": "unix",
                "platformOs": "linux",
                "userAgent": "mock",
            },
        })
    elif method == "initialized":
        continue
    elif method == "thread/start":
        send({"id": request_id, "result": {"thread": {"id": "thread-1"}}})
    elif method == "turn/start":
        send({"id": request_id, "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}})
        if fail_turn:
            send({"method": "TurnFailedNotification", "params": {"message": "turn exploded"}})
        else:
            send({
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1", "delta": "hello"},
            })
            send({
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1", "delta": " world"},
            })
            send({
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed", "items": []}},
            })
    elif method == "thread/archive":
        send({"id": request_id, "result": {}})
"""


def _mock_server(tmp_path: Path) -> Path:
    path = tmp_path / "mock_codex_app_server.py"
    path.write_text(MOCK_SERVER, encoding="utf-8")
    return path


def test_stdio_client_returns_streamed_agent_message(tmp_path: Path) -> None:
    script = _mock_server(tmp_path)

    async def scenario() -> str:
        client = CodexAppServerClient(command=[sys.executable, str(script)])
        try:
            thread_id = await client.start_thread("gpt-5.5", "xhigh", tmp_path, "base")
            return await client.send_turn(thread_id, "prompt", 5.0)
        finally:
            await client.close()

    assert asyncio.run(scenario()) == "hello world"


def test_stdio_client_raises_turn_error_on_failed_notification(tmp_path: Path) -> None:
    script = _mock_server(tmp_path)

    async def scenario() -> None:
        client = CodexAppServerClient(command=[sys.executable, str(script), "--fail-turn"])
        try:
            thread_id = await client.start_thread("gpt-5.5", "xhigh", tmp_path, None)
            await client.send_turn(thread_id, "prompt", 5.0)
        finally:
            await client.close()

    with pytest.raises(CodexAppServerTurnError, match="turn exploded"):
        asyncio.run(scenario())
