"""Attach-only launcher for an already running CDP browser."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, ClassVar

from codd.deployment.providers.verification.cdp_launchers import (
    CdpLauncher,
    register_cdp_launcher,
)


@register_cdp_launcher("external_running")
class ExternalRunningLauncher(CdpLauncher):
    """Use a browser that is started outside CoDD."""

    launcher_name: ClassVar[str] = "external_running"

    def launch_command(self, browser_config: Mapping[str, Any]) -> list[str]:
        return []

    def teardown_command(self) -> list[str]:
        return []

    def is_alive(self, browser_config: Mapping[str, Any]) -> bool:
        return _version_endpoint_reachable(browser_config)


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


def _version_endpoint_reachable(config: Mapping[str, Any]) -> bool:
    try:
        with urllib.request.urlopen(_version_endpoint(config), timeout=float(config.get("timeout", 1.0))):
            return True
    except (OSError, ValueError, urllib.error.URLError):
        return False
