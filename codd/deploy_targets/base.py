from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DeployTarget(ABC):
    """Abstract base for deploy targets. Implement snapshot/deploy/healthcheck/rollback."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Capture current state for rollback."""

    @abstractmethod
    def dry_run(self) -> list[str]:
        """Return list of proposed actions without executing."""

    @abstractmethod
    def deploy(self) -> bool:
        """Execute deploy. Return True on success."""

    @abstractmethod
    def healthcheck(self) -> bool:
        """Run healthcheck. Return True if healthy."""

    @abstractmethod
    def rollback(self, snapshot: dict[str, Any]) -> bool:
        """Restore to snapshot state. Return True on success."""
