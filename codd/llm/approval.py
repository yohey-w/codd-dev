"""Approval state helpers for LLM-derived considerations."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable, Literal, Mapping

import yaml

from codd.deployment.providers.llm_consideration import Consideration, parse_considerations


class ApprovalState(str, Enum):
    """Allowed approval states for one generated consideration."""

    pending = "pending"
    approved = "approved"
    skipped = "skipped"


ApprovalStateValue = Literal["pending", "approved", "skipped"]
ApprovalMode = Literal["required", "per_consideration", "auto"]

APPROVAL_STATES: set[str] = {state.value for state in ApprovalState}
APPROVAL_MODES: set[str] = {"required", "per_consideration", "auto"}
DEFAULT_PENDING_NOTIFICATION_THRESHOLD = 5


class ApprovalCache:
    """Persist per-consideration approval state under ``.codd``."""

    @staticmethod
    def cache_path(consideration_id: str, cache_dir: Path | str) -> Path:
        directory = _approval_dir(cache_dir)
        return directory / f"{_safe_id(consideration_id)}.json"

    @classmethod
    def save(cls, consideration_id: str, status: ApprovalState | ApprovalStateValue | str, cache_dir: Path | str) -> Path:
        """Write one approval decision and return the written path."""

        normalized = _approval_state(status)
        path = cls.cache_path(consideration_id, cache_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "consideration_id": consideration_id,
                    "status": normalized,
                    "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, consideration_id: str, cache_dir: Path | str) -> ApprovalStateValue:
        """Return one stored state, defaulting to ``pending``."""

        path = cls.cache_path(consideration_id, cache_dir)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "pending"
        return _approval_state(payload.get("status"))

    @staticmethod
    def load_all(cache_dir: Path | str) -> dict[str, ApprovalStateValue]:
        """Return all stored approval states keyed by original consideration id."""

        directory = _approval_dir(cache_dir)
        states: dict[str, ApprovalStateValue] = {}
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            consideration_id = str(payload.get("consideration_id") or path.stem)
            states[consideration_id] = _approval_state(payload.get("status"))
        return states


def effective_approval_mode(
    mode: str | None,
    *,
    require_explicit_optin: bool = False,
) -> ApprovalMode:
    """Apply the explicit opt-in guard for automatic approval."""

    requested = str(mode or "required")
    if requested not in APPROVAL_MODES:
        requested = "required"
    if requested == "auto" and not require_explicit_optin:
        return "required"
    return requested  # type: ignore[return-value]


def filter_approved(
    considerations: list[Consideration],
    mode: ApprovalMode | str,
    *,
    cache_dir: Path | str | None = None,
    require_explicit_optin: bool = False,
) -> list[Consideration]:
    """Return considerations allowed by the selected approval mode."""

    effective_mode = effective_approval_mode(mode, require_explicit_optin=require_explicit_optin)
    if effective_mode == "auto":
        return list(considerations)

    cached = ApprovalCache.load_all(cache_dir) if cache_dir is not None else {}
    return [
        consideration
        for consideration in considerations
        if _status_for(consideration, cached) == "approved"
    ]


def pending_considerations(
    considerations: list[Consideration],
    *,
    cache_dir: Path | str | None = None,
) -> list[Consideration]:
    """Return considerations that are not approved or skipped."""

    cached = ApprovalCache.load_all(cache_dir) if cache_dir is not None else {}
    return [
        consideration
        for consideration in considerations
        if _status_for(consideration, cached) == "pending"
    ]


def load_cached_considerations(project_root: Path | str) -> list[Consideration]:
    """Load generated considerations from known cache locations."""

    root = Path(project_root)
    candidates = [
        root / ".codd" / "consideration_cache",
        root / ".codd" / "derived_considerations",
    ]
    loaded: dict[str, Consideration] = {}
    for directory in candidates:
        if not directory.is_dir():
            continue
        for path in sorted([*directory.glob("*.json"), *directory.glob("*.yaml"), *directory.glob("*.yml")]):
            for consideration in _considerations_from_file(path):
                loaded[consideration.id] = consideration
    return list(loaded.values())


def notification_threshold(config: Mapping[str, Any] | None = None) -> int:
    """Resolve the notification threshold from config or return the default."""

    value = _nested_value(config, ("llm", "approval_notification_threshold"))
    if value is None:
        value = _nested_value(config, ("notification", "approval_notification_threshold"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_PENDING_NOTIFICATION_THRESHOLD


def notify_pending_considerations(
    considerations: list[Consideration],
    config: Mapping[str, Any] | None = None,
    *,
    threshold: int | None = None,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    output: Callable[[str], None] = print,
) -> bool:
    """Notify when pending consideration count exceeds the configured threshold."""

    count = len([item for item in considerations if _approval_state(item.approval_status) == "pending"])
    limit = notification_threshold(config) if threshold is None else int(threshold)
    if count <= limit:
        return False

    message = f"CoDD LLM approval required: {count} pending considerations"
    command = _ntfy_command(config)
    if not command:
        output(message)
        return False

    prepared = _prepare_ntfy_command(command, message)
    completed = run_command(prepared, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        output(message)
        return False
    return True


def consideration_status(
    consideration: Consideration,
    cache_dir: Path | str | None = None,
) -> ApprovalStateValue:
    """Return persisted status if available, otherwise the generated status."""

    cached = ApprovalCache.load_all(cache_dir) if cache_dir is not None else {}
    return _status_for(consideration, cached)


def consideration_to_dict(consideration: Consideration) -> dict[str, Any]:
    """Serialize a consideration for CLI output."""

    return asdict(consideration)


def require_auto_optin(config: Mapping[str, Any] | None = None) -> bool:
    """Return whether automatic approval is explicitly enabled in config."""

    return any(
        bool(_nested_value(config, path))
        for path in (
            ("llm", "allow_auto", "require_explicit_optin"),
            ("llm", "approval_mode_auto", "require_explicit_optin"),
            ("llm", "require_explicit_optin"),
        )
    )


def approval_mode_from_config(config: Mapping[str, Any] | None = None) -> ApprovalMode:
    """Return the guarded approval mode from project config."""

    return effective_approval_mode(
        _nested_value(config, ("llm", "approval_mode")),
        require_explicit_optin=require_auto_optin(config),
    )


def _considerations_from_file(path: Path) -> list[Consideration]:
    try:
        if path.suffix == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError, ValueError):
        return []

    if isinstance(payload, Mapping) and isinstance(payload.get("considerations"), list):
        raw = {"considerations": payload["considerations"]}
    elif isinstance(payload, list):
        raw = {"considerations": payload}
    else:
        return []

    try:
        return parse_considerations(json.dumps(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def _status_for(
    consideration: Consideration,
    cached: Mapping[str, ApprovalStateValue],
) -> ApprovalStateValue:
    if consideration.id in cached:
        return cached[consideration.id]
    return _approval_state(consideration.approval_status)


def _approval_state(value: Any) -> ApprovalStateValue:
    text = value.value if isinstance(value, ApprovalState) else str(value or "pending")
    return text if text in APPROVAL_STATES else "pending"  # type: ignore[return-value]


def _approval_dir(cache_dir: Path | str) -> Path:
    base = Path(cache_dir)
    if base.name == "consideration_approvals":
        return base
    if base.name == ".codd":
        return base / "consideration_approvals"
    return base / ".codd" / "consideration_approvals"


def _safe_id(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value).strip("._")
    return safe or "consideration"


def _nested_value(config: Mapping[str, Any] | None, path: tuple[str, ...]) -> Any:
    value: Any = config or {}
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _ntfy_command(config: Mapping[str, Any] | None) -> str | None:
    value = os.environ.get("CODD_NTFY_COMMAND") or _nested_value(config, ("notification", "ntfy_command"))
    return str(value).strip() if value else None


def _prepare_ntfy_command(command: str, message: str) -> list[str]:
    if "{message}" in command:
        return shlex.split(command.format(message=message))
    return [*shlex.split(command), message]


__all__ = [
    "ApprovalCache",
    "ApprovalMode",
    "ApprovalState",
    "approval_mode_from_config",
    "consideration_status",
    "consideration_to_dict",
    "effective_approval_mode",
    "filter_approved",
    "load_cached_considerations",
    "notification_threshold",
    "notify_pending_considerations",
    "pending_considerations",
    "require_auto_optin",
]
