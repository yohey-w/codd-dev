"""Subprocess-backed AI command adapter for deployment providers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable, Mapping, Protocol

from codd.config import load_project_config
from codd.defaults import AI_TIMEOUT_SECONDS as _DEFAULT_AI_TIMEOUT_SECONDS


DEFAULT_AI_COMMAND = "ai"
# Default sourced from codd.defaults so every AI call site shares one SSoT
# (see feedback_codd_default_values_policy). Override via the
# CODD_AI_TIMEOUT_SECONDS env var or `llm.timeout_seconds` in codd.yaml.
DEFAULT_TIMEOUT_SECONDS = _DEFAULT_AI_TIMEOUT_SECONDS

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class AiCommandError(RuntimeError):
    """Base error raised by the AI command adapter."""


class AiCommandTimeout(AiCommandError):
    """Raised when the AI command exceeds the configured timeout."""


class AiCommand(Protocol):
    """Common synchronous AI command interface."""

    def invoke(self, prompt: str, model: str | None = None, timeout: float | None = None) -> str:
        """Invoke the AI command and return its text output."""

    def close(self) -> None:
        """Release any resources held by the command."""


@dataclass
class SubprocessAiCommand:
    """Invoke an AI CLI via subprocess without depending on an SDK."""

    command: str | None = None
    project_root: Path | str | None = None
    timeout: float | None = None
    config: Mapping[str, Any] | None = None
    runner: RunCommand = subprocess.run

    def invoke(self, prompt: str, model: str | None = None, timeout: float | None = None) -> str:
        config = _load_config(self.project_root, self.config)
        command = resolve_command(config, self.command)
        resolved_model = resolve_model(config, model)
        resolved_timeout = resolve_timeout(config, timeout if timeout is not None else self.timeout)
        prepared = _prepare_command(command, resolved_model)

        try:
            completed = self.runner(
                prepared,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=resolved_timeout,
                check=False,
                cwd=str(self.project_root) if self.project_root is not None else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise AiCommandTimeout(f"AI command timed out after {resolved_timeout:g}s") from exc
        except FileNotFoundError as exc:
            raise AiCommandError(f"AI command not found: {prepared[0]}") from exc

        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise AiCommandError(f"AI command failed: {detail}")
        if not completed.stdout.strip():
            raise AiCommandError("AI command returned empty output")
        return completed.stdout

    def provider_id(self, model: str | None = None) -> str:
        config = _load_config(self.project_root, self.config)
        command = resolve_command(config, self.command)
        resolved_model = resolve_model(config, model) or ""
        digest = hashlib.sha256(f"{' '.join(command)}\0{resolved_model}".encode("utf-8")).hexdigest()
        return f"subprocess_ai_command:{digest[:12]}"

    def close(self) -> None:
        """No-op for subprocess commands."""


class CodexAppServerAiCommand:
    """Synchronous AiCommand wrapper backed by Codex App Server JSON-RPC."""

    def __init__(
        self,
        client: Any,
        *,
        project_root: Path | str | None = None,
        model: str | None = None,
        effort: str | None = "xhigh",
        timeout_seconds: float = 300.0,
        thread_strategy: str = "per_session",
        base_instructions: str | None = None,
        fallback: str = "subprocess",
        fallback_command: AiCommand | None = None,
    ) -> None:
        self.client = client
        self.project_root = Path(project_root) if project_root is not None else None
        self.model = model
        self.effort = effort
        self.timeout_seconds = float(timeout_seconds)
        self.thread_strategy = thread_strategy
        self.base_instructions = base_instructions
        self.fallback = fallback
        self.fallback_command = fallback_command or SubprocessAiCommand(project_root=self.project_root)
        self._thread_id: str | None = None
        self._loop: Any | None = None

    @classmethod
    def from_config(cls, section: Mapping[str, Any], project_root: Path | str | None) -> "CodexAppServerAiCommand":
        from codd.deployment.providers.codex_app_server import (
            DEFAULT_APP_SERVER_COMMAND,
            CodexAppServerClient,
            CodexAppServerInitError,
        )

        config = _mapping(section)
        transport = str(config.get("transport") or "stdio")
        if transport != "stdio":
            raise CodexAppServerInitError(f"unsupported Codex App Server transport: {transport}")
        thread_strategy = str(config.get("thread_strategy") or "per_session")
        if thread_strategy not in {"per_cmd", "per_session"}:
            raise CodexAppServerInitError(f"unsupported Codex App Server thread_strategy: {thread_strategy}")
        timeout = _float_or_default(config.get("timeout_seconds"), 300.0)
        command = config.get("command") or DEFAULT_APP_SERVER_COMMAND
        framing = str(config.get("framing") or "json_lines")
        client = CodexAppServerClient(
            command=str(command),
            transport=transport,
            cwd=project_root,
            framing=framing,
        )
        return cls(
            client,
            project_root=project_root,
            model=str(config.get("model") or "gpt-5.5"),
            effort=str(config.get("effort") or "xhigh"),
            timeout_seconds=timeout,
            thread_strategy=thread_strategy,
            base_instructions=_optional_str(config.get("base_instructions")),
            fallback=str(config.get("fallback") or "subprocess"),
        )

    def invoke(self, prompt: str, model: str | None = None, timeout: float | None = None) -> str:
        from codd.deployment.providers.codex_app_server import (
            CodexAppServerInitError,
            CodexAppServerTimeout,
            CodexAppServerTurnError,
        )

        try:
            return str(self._run_async(self._invoke_app_server(prompt, model=model, timeout=timeout)))
        except (CodexAppServerInitError, CodexAppServerTimeout, CodexAppServerTurnError) as exc:
            if self.fallback == "silent":
                return ""
            if self.fallback == "subprocess":
                import logging

                logging.getLogger(__name__).warning("Codex App Server fallback: %s", exc)
                return self.fallback_command.invoke(prompt, model=model, timeout=timeout)
            raise

    def close(self) -> None:
        try:
            if self._thread_id is not None:
                self._run_async(self.client.archive_thread(self._thread_id))
                self._thread_id = None
            self._run_async(self.client.close())
        finally:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.close()
            self._loop = None

    def provider_id(self, model: str | None = None) -> str:
        resolved_model = model or self.model or ""
        digest = hashlib.sha256(
            f"codex_app_server\0{self.thread_strategy}\0{resolved_model}".encode("utf-8")
        ).hexdigest()
        return f"codex_app_server:{digest[:12]}"

    async def _invoke_app_server(self, prompt: str, *, model: str | None, timeout: float | None) -> str:
        resolved_model = model or self.model
        resolved_timeout = float(timeout if timeout is not None else self.timeout_seconds)
        if self.thread_strategy == "per_cmd":
            thread_id = await self.client.start_thread(
                resolved_model,
                self.effort,
                self.project_root,
                self.base_instructions,
            )
            try:
                return await self.client.send_turn(
                    thread_id,
                    prompt,
                    resolved_timeout,
                    model=resolved_model,
                    effort=self.effort,
                    cwd=self.project_root,
                )
            finally:
                await self.client.archive_thread(thread_id)
        thread_id = await self._ensure_thread(resolved_model)
        return await self.client.send_turn(
            thread_id,
            prompt,
            resolved_timeout,
            model=resolved_model,
            effort=self.effort,
            cwd=self.project_root,
        )

    async def _ensure_thread(self, model: str | None) -> str:
        if self._thread_id is None:
            self._thread_id = await self.client.start_thread(
                model,
                self.effort,
                self.project_root,
                self.base_instructions,
            )
        return self._thread_id

    def _run_async(self, awaitable: Any) -> Any:
        try:
            asyncio = __import__("asyncio")
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AiCommandError("CodexAppServerAiCommand cannot be invoked from an active event loop")
        if self._loop is None or self._loop.is_closed():
            asyncio = __import__("asyncio")
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(awaitable)


def invoke(prompt: str, model: str | None = None) -> str:
    """Invoke the default subprocess AI command."""

    return SubprocessAiCommand().invoke(prompt, model=model)


def resolve_command(config: Mapping[str, Any] | None = None, command: str | None = None) -> list[str]:
    """Resolve the command from explicit input, environment, or project config."""

    raw_command = (
        command
        or os.environ.get("CODD_AI_COMMAND")
        or _nested_str(config, ("llm", "command"))
        or _nested_str(config, ("ai_commands", "derive_considerations"))
        or _mapping(config).get("ai_command")
        or DEFAULT_AI_COMMAND
    )
    if not isinstance(raw_command, str) or not raw_command.strip():
        raise ValueError("ai_command must be a non-empty string")
    parts = shlex.split(raw_command.strip())
    if not parts:
        raise ValueError("ai_command must not be empty")
    return parts


def resolve_model(config: Mapping[str, Any] | None = None, model: str | None = None) -> str | None:
    """Resolve the model from explicit input, environment, or ``llm.model``."""

    raw_model = model or os.environ.get("CODD_LLM_MODEL") or _nested_str(config, ("llm", "model"))
    if raw_model is None:
        return None
    resolved = str(raw_model).strip()
    return resolved or None


def resolve_timeout(config: Mapping[str, Any] | None = None, timeout: float | None = None) -> float:
    """Resolve timeout seconds from explicit input, environment, or config."""

    if timeout is not None:
        return float(timeout)
    env_timeout = os.environ.get("CODD_AI_TIMEOUT_SECONDS")
    if env_timeout:
        return _float_or_default(env_timeout, DEFAULT_TIMEOUT_SECONDS)
    return _float_or_default(_nested_value(config, ("llm", "timeout_seconds")), DEFAULT_TIMEOUT_SECONDS)


def _prepare_command(command: list[str], model: str | None) -> list[str]:
    if model is None:
        return command
    prepared = [part.replace("{model}", model) for part in command]
    if prepared != command or _has_model_arg(prepared):
        return prepared
    return [*prepared, "--model", model]


def _has_model_arg(command: list[str]) -> bool:
    return any(part in {"--model", "-m"} or part.startswith("--model=") for part in command)


def _load_config(
    project_root: Path | str | None,
    config: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if config is not None:
        return config
    if project_root is None:
        return None
    try:
        return load_project_config(Path(project_root))
    except (FileNotFoundError, ValueError):
        return None


def _mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested_value(config: Mapping[str, Any] | None, path: tuple[str, ...]) -> Any:
    value: Any = _mapping(config)
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def _nested_str(config: Mapping[str, Any] | None, path: tuple[str, ...]) -> str | None:
    value = _nested_value(config, path)
    return value if isinstance(value, str) else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "AiCommand",
    "AiCommandError",
    "AiCommandTimeout",
    "CodexAppServerAiCommand",
    "DEFAULT_AI_COMMAND",
    "DEFAULT_TIMEOUT_SECONDS",
    "SubprocessAiCommand",
    "invoke",
    "resolve_command",
    "resolve_model",
    "resolve_timeout",
]
