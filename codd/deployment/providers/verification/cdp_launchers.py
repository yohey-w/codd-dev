"""CDP launcher registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Mapping


class CdpLauncher(ABC):
    """Provide launch, teardown, and liveness commands for a CDP target."""

    launcher_name: ClassVar[str]

    @abstractmethod
    def launch_command(self, browser_config: Mapping[str, Any]) -> list[str]:
        """Return the command used to start the target."""

    @abstractmethod
    def teardown_command(self) -> list[str]:
        """Return the command used to stop the target."""

    @abstractmethod
    def is_alive(self, browser_config: Mapping[str, Any]) -> bool:
        """Return whether the target is already reachable."""


CDP_LAUNCHERS: dict[str, type[CdpLauncher]] = {}


def register_cdp_launcher(name: str):
    """Register a CDP launcher class under ``name``."""

    def decorator(cls: type[CdpLauncher]) -> type[CdpLauncher]:
        CDP_LAUNCHERS[name] = cls
        return cls

    return decorator


__all__ = [
    "CDP_LAUNCHERS",
    "CdpLauncher",
    "register_cdp_launcher",
]
