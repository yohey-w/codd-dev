"""Chromium CDP browser engine for the cookbook."""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Mapping
from typing import Any, ClassVar

from codd.deployment.providers.verification.cdp_engines import (
    BrowserEngine,
    register_browser_engine,
)


@register_browser_engine("chromium")
class ChromiumBrowserEngine(BrowserEngine):
    """Resolve a Chromium debug endpoint to a WebSocket debugger URL."""

    engine_name: ClassVar[str] = "chromium"

    def cdp_endpoint(self, config: Mapping[str, Any]) -> str:
        return _websocket_debugger_url(config)

    def normalized_capabilities(self) -> set[str]:
        return {"javascript", "cookies", "storage", "dom", "navigation"}


def _websocket_debugger_url(config: Mapping[str, Any]) -> str:
    direct = config.get("websocket_url") or os.environ.get(str(config.get("websocket_url_env") or "CODD_CDP_WS_URL"))
    if direct:
        return str(direct)

    with urllib.request.urlopen(_version_endpoint(config), timeout=float(config.get("timeout", 2.0))) as response:
        payload = json.loads(response.read().decode("utf-8"))
    endpoint = payload.get("webSocketDebuggerUrl")
    if not endpoint:
        raise ValueError("CDP version endpoint did not return webSocketDebuggerUrl")
    return str(endpoint)


def _version_endpoint(config: Mapping[str, Any]) -> str:
    endpoint_env = str(config.get("version_url_env") or "CODD_CDP_VERSION_URL")
    endpoint = config.get("version_url") or os.environ.get(endpoint_env)
    if endpoint:
        return str(endpoint)

    host_env = str(config.get("host_env") or "CODD_CDP_HOST")
    port_env = str(config.get("port_env") or "CODD_CDP_PORT")
    host = config.get("host") or os.environ.get(host_env)
    port = config.get("port") or os.environ.get(port_env)
    if not host or not port:
        raise ValueError("CDP version endpoint requires version_url or host and port")
    scheme = str(config.get("scheme") or os.environ.get("CODD_CDP_SCHEME") or "http")
    return f"{scheme}://{host}:{port}/json/version"
