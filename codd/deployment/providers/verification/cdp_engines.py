"""CDP browser engine registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Mapping


class BrowserEngine(ABC):
    """Resolve a browser-like runtime to a CDP endpoint and capabilities."""

    engine_name: ClassVar[str]

    @abstractmethod
    def cdp_endpoint(self, config: Mapping[str, Any]) -> str:
        """Return the endpoint used by the CDP client."""

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


__all__ = [
    "BROWSER_ENGINES",
    "BrowserEngine",
    "register_browser_engine",
]
