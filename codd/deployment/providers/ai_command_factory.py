"""Factory for selecting an AI command transport."""

from __future__ import annotations

import logging
from pathlib import Path
import shlex
import shutil
from typing import Any, Mapping

from codd.deployment.providers.ai_command import (
    AiCommand,
    CodexAppServerAiCommand,
    SubprocessAiCommand,
)
from codd.deployment.providers.codex_app_server import (
    DEFAULT_APP_SERVER_COMMAND,
    CodexAppServerInitError,
)


LOGGER = logging.getLogger(__name__)


def get_ai_command(
    config: Mapping[str, Any] | None,
    project_root: Path | str | None = None,
    command_override: str | None = None,
) -> AiCommand:
    """Return the configured AI command adapter, falling back to subprocess by default."""

    section = _section(config)
    if not bool(section.get("enabled", False)):
        return _subprocess(config, project_root, command_override)

    transport = str(section.get("transport") or "stdio")
    fallback = str(section.get("fallback") or "subprocess")
    if transport == "stdio":
        binary = _command_binary(str(section.get("command") or DEFAULT_APP_SERVER_COMMAND))
        if shutil.which(binary) is None:
            return _fallback(
                fallback,
                f"Codex App Server binary not found: {binary}",
                config,
                project_root,
                command_override,
            )
    elif transport == "unix":
        socket_path = _unix_socket_path(section.get("url"))
        if socket_path is None or not socket_path.exists():
            return _fallback(
                fallback,
                f"Codex App Server unix socket not reachable: {section.get('url') or '(missing url)'}",
                config,
                project_root,
                command_override,
            )
    elif transport != "ws":
        return _fallback(
            fallback,
            f"unsupported Codex App Server transport: {transport}",
            config,
            project_root,
            command_override,
        )

    try:
        adapter = CodexAppServerAiCommand.from_config(section, project_root)
    except CodexAppServerInitError as exc:
        return _fallback(fallback, str(exc), config, project_root, command_override, exc=exc)
    except (OSError, ValueError) as exc:
        return _fallback(fallback, str(exc), config, project_root, command_override)

    if isinstance(adapter, CodexAppServerAiCommand):
        adapter.fallback = fallback
        adapter.fallback_command = _subprocess(config, project_root, command_override)
    return adapter


def _section(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    raw = config.get("codex_app_server") if isinstance(config, Mapping) else None
    return raw if isinstance(raw, Mapping) else {}


def _subprocess(
    config: Mapping[str, Any] | None,
    project_root: Path | str | None,
    command_override: str | None,
) -> SubprocessAiCommand:
    return SubprocessAiCommand(command=command_override, project_root=project_root, config=config)


def _fallback(
    fallback: str,
    reason: str,
    config: Mapping[str, Any] | None,
    project_root: Path | str | None,
    command_override: str | None,
    *,
    exc: Exception | None = None,
) -> AiCommand:
    if fallback == "silent":
        return _SilentAiCommand()
    if fallback == "subprocess":
        LOGGER.warning("Codex App Server fallback: %s", reason)
        return _subprocess(config, project_root, command_override)
    if isinstance(exc, CodexAppServerInitError):
        raise exc
    raise CodexAppServerInitError(reason)


def _command_binary(command: str) -> str:
    parts = shlex.split(command)
    return parts[0] if parts else "codex"


def _unix_socket_path(url: Any) -> Path | None:
    if not isinstance(url, str) or not url:
        return None
    raw_path = url.removeprefix("unix://")
    return Path(raw_path) if raw_path else None


class _SilentAiCommand:
    def invoke(self, prompt: str, model: str | None = None, timeout: float | None = None) -> str:
        return ""

    def close(self) -> None:
        return None


__all__ = ["get_ai_command"]
