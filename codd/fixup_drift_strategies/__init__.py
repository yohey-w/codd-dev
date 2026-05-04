"""Fix strategy registry for ``codd fixup-drift``.

Concrete strategies are expected to live in separate modules and register
themselves with ``@register_strategy``.  Phase 1 intentionally ships only the
registry and base contracts so later strategy work can plug in without changing
the CLI plumbing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Optional


@dataclass
class FixProposal:
    """A proposed fix rendered in dry-run output and optionally applied later."""

    kind: str
    file_path: str
    diff: str
    description: str
    severity: str
    can_auto_apply: bool


class BaseFixStrategy(ABC):
    """Base class for all fixup-drift strategies."""

    KIND: str = ""

    def __init__(self, project_root: Path):
        self.project_root = project_root

    @abstractmethod
    def propose(self, event) -> list[FixProposal]:
        """Return one or more fix proposals for a DriftEvent."""

    @abstractmethod
    def apply(self, proposal: FixProposal) -> bool:
        """Apply a proposal. Called inside an isolated git worktree."""


_REGISTRY: dict[str, type[BaseFixStrategy]] = {}


def register_strategy(cls: type[BaseFixStrategy]) -> type[BaseFixStrategy]:
    """Register a strategy class by its ``KIND`` value."""
    if not cls.KIND:
        raise ValueError("Fix strategies must define KIND")
    _REGISTRY[cls.KIND] = cls
    return cls


def get_strategy(kind: str, project_root: Path) -> Optional[BaseFixStrategy]:
    """Return a strategy instance for ``kind`` when one is registered."""
    cls = _REGISTRY.get(kind)
    return cls(project_root) if cls else None


def list_registered_kinds() -> list[str]:
    """Return registered strategy kinds in stable order."""
    return sorted(_REGISTRY)


# Auto-register concrete strategies.
for _module_name in ("url_drift", "design_token_drift", "lexicon_violation"):
    _qualified_name = f"{__name__}.{_module_name}"
    try:
        import_module(_qualified_name)
    except ModuleNotFoundError as exc:
        if exc.name != _qualified_name:
            raise
