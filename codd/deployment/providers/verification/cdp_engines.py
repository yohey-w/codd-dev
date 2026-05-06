"""CDP browser engine registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping


class BrowserEngine(ABC):
    """Resolve a browser-like runtime to a CDP endpoint and capabilities."""

    engine_name: ClassVar[str]

    @abstractmethod
    def cdp_endpoint(self, config: Mapping[str, Any]) -> str:
        """Return the endpoint used by the CDP peer."""

    @abstractmethod
    def normalized_capabilities(self) -> set[str]:
        """Return capability names exposed by this engine."""


BROWSER_ENGINES: dict[str, type[BrowserEngine]] = {}


def register_browser_engine(name: str):
    """Register a browser engine class under ``name``."""

    def decorator(cls: type[BrowserEngine]) -> type[BrowserEngine]:
        BROWSER_ENGINES[name] = cls
        return cls

    return decorator


@dataclass(frozen=True)
class CdpRuntimeCommand:
    """One CDP command derived from a runtime variant attribute mapping."""

    method: str
    params: dict[str, Any]


def runtime_commands_for_attributes(attributes: Mapping[str, Any]) -> list[CdpRuntimeCommand]:
    """Translate declarative runtime attributes into CDP wire commands."""

    commands: list[CdpRuntimeCommand] = []
    metrics = _metrics_params(attributes)
    if metrics is not None:
        commands.append(CdpRuntimeCommand("Emulation.setDeviceMetricsOverride", metrics))

    locale = _string_attribute(attributes, "locale", "language")
    if locale:
        commands.append(CdpRuntimeCommand("Emulation.setLocaleOverride", {"locale": locale}))

    timezone = _string_attribute(attributes, "timezone", "timezone_id", "timezoneId")
    if timezone:
        commands.append(CdpRuntimeCommand("Emulation.setTimezoneOverride", {"timezoneId": timezone}))

    user_agent = _string_attribute(attributes, "user_agent", "userAgent")
    if user_agent:
        commands.append(CdpRuntimeCommand("Network.setUserAgentOverride", {"userAgent": user_agent}))

    commands.extend(_declared_commands(attributes.get("cdp_commands")))
    return commands


def _metrics_params(attributes: Mapping[str, Any]) -> dict[str, Any] | None:
    has_width = "width" in attributes
    has_height = "height" in attributes
    if not has_width and not has_height:
        return None
    if not has_width or not has_height:
        raise ValueError("runtime attributes width and height must be provided together")

    params: dict[str, Any] = {
        "width": _int_attribute(attributes, "width"),
        "height": _int_attribute(attributes, "height"),
        "deviceScaleFactor": _float_attribute(
            attributes,
            "device_scale_factor",
            "deviceScaleFactor",
            default=1.0,
        ),
        "mobile": _bool_attribute(attributes, "mobile", default=False),
    }
    orientation = _mapping_attribute(attributes.get("screen_orientation") or attributes.get("screenOrientation"))
    if orientation:
        params["screenOrientation"] = orientation
    return params


def _declared_commands(value: Any) -> list[CdpRuntimeCommand]:
    if not isinstance(value, list):
        return []

    commands: list[CdpRuntimeCommand] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("cdp_commands entries must be mappings")
        method = str(item.get("method") or "").strip()
        if not method:
            raise ValueError("cdp_commands entries require method")
        params = item.get("params", {})
        if not isinstance(params, Mapping):
            raise ValueError("cdp_commands params must be a mapping")
        commands.append(CdpRuntimeCommand(method, dict(params)))
    return commands


def _string_attribute(attributes: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = attributes.get(name)
        if value is not None and value != "":
            return str(value)
    return ""


def _int_attribute(attributes: Mapping[str, Any], name: str) -> int:
    try:
        return int(attributes[name])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"runtime attribute {name} must be an integer") from exc


def _float_attribute(
    attributes: Mapping[str, Any],
    *names: str,
    default: float,
) -> float:
    for name in names:
        if name not in attributes:
            continue
        try:
            return float(attributes[name])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"runtime attribute {name} must be numeric") from exc
    return default


def _bool_attribute(attributes: Mapping[str, Any], name: str, *, default: bool) -> bool:
    value = attributes.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _mapping_attribute(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = [
    "BROWSER_ENGINES",
    "BrowserEngine",
    "CdpRuntimeCommand",
    "register_browser_engine",
    "runtime_commands_for_attributes",
]
