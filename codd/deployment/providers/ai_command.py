"""Subprocess-backed AI command adapter for deployment providers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable, Mapping

from codd.config import load_project_config


DEFAULT_AI_COMMAND = "ai"
DEFAULT_TIMEOUT_SECONDS = 120.0

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class AiCommandError(RuntimeError):
    """Base error raised by the AI command adapter."""


class AiCommandTimeout(AiCommandError):
    """Raised when the AI command exceeds the configured timeout."""


@dataclass
class SubprocessAiCommand:
    """Invoke an AI CLI via subprocess without depending on an SDK."""

    command: str | None = None
    project_root: Path | str | None = None
    timeout: float | None = None
    config: Mapping[str, Any] | None = None
    runner: RunCommand = subprocess.run

    def invoke(self, prompt: str, model: str | None = None) -> str:
        config = _load_config(self.project_root, self.config)
        command = resolve_command(config, self.command)
        resolved_model = resolve_model(config, model)
        timeout = resolve_timeout(config, self.timeout)
        prepared = _prepare_command(command, resolved_model)

        try:
            completed = self.runner(
                prepared,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                check=False,
                cwd=str(self.project_root) if self.project_root is not None else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise AiCommandTimeout(f"AI command timed out after {timeout:g}s") from exc
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


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "AiCommandError",
    "AiCommandTimeout",
    "DEFAULT_AI_COMMAND",
    "DEFAULT_TIMEOUT_SECONDS",
    "SubprocessAiCommand",
    "invoke",
    "resolve_command",
    "resolve_model",
    "resolve_timeout",
]
