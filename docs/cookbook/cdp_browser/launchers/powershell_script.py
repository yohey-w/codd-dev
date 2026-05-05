"""PowerShell script launcher for the CDP browser cookbook."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from codd.deployment.providers.verification.cdp_launchers import (
    CdpLauncher,
    register_cdp_launcher,
)


@register_cdp_launcher("powershell_script")
class PowerShellScriptLauncher(CdpLauncher):
    """Launch a CDP-enabled browser through a project-owned PowerShell script."""

    launcher_name: ClassVar[str] = "powershell_script"

    def __init__(self) -> None:
        self._last_config: dict[str, Any] = {}

    def launch_command(self, browser_config: Mapping[str, Any]) -> list[str]:
        self._last_config = dict(browser_config)
        script_path = _env_required(
            browser_config,
            key="script_path_env",
            default_env="CODD_CDP_POWERSHELL_SCRIPT",
        )
        executable = str(browser_config.get("executable") or "pwsh")
        shell_args = _string_list(
            browser_config.get(
                "powershell_args",
                ["-NoProfile", "-ExecutionPolicy", "Bypass"],
            )
        )
        return [executable, *shell_args, "-File", script_path, *_string_list(browser_config.get("args", []))]

    def teardown_command(self) -> list[str]:
        explicit = _string_list(self._last_config.get("teardown_command", []))
        if explicit:
            return explicit
        executable = str(self._last_config.get("executable") or "pwsh")
        return [
            executable,
            "-NoProfile",
            "-Command",
            (
                "$pidValue = [Environment]::GetEnvironmentVariable('CODD_CDP_BROWSER_PID'); "
                "if ($pidValue) { Stop-Process -Id $pidValue -ErrorAction SilentlyContinue }"
            ),
        ]

    def is_alive(self, browser_config: Mapping[str, Any]) -> bool:
        return _version_endpoint_reachable(browser_config)


def _env_required(config: Mapping[str, Any], *, key: str, default_env: str) -> str:
    env_name = str(config.get(key) or default_env)
    value = os.environ.get(env_name)
    if not value:
        raise ValueError(f"environment variable is required: {env_name}")
    return value


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(item) for item in value]
    raise TypeError("command arguments must be a string or a sequence")


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
