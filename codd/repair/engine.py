"""Repair engine registry and base contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, TYPE_CHECKING

if TYPE_CHECKING:
    from codd.dag import DAG
    from codd.repair.schema import (
        ApplyResult,
        RepairProposal,
        RootCauseAnalysis,
        VerificationFailureReport,
    )


_REPAIR_ENGINES: dict[str, type["RepairEngine"]] = {}


def register_repair_engine(name: str):
    """Register a repair engine class under ``name``."""

    normalized = _normalize_engine_name(name)

    def decorator(cls: type["RepairEngine"]) -> type["RepairEngine"]:
        if normalized in _REPAIR_ENGINES:
            raise KeyError(f"repair engine already registered: {normalized}")
        _REPAIR_ENGINES[normalized] = cls
        cls.engine_name = normalized
        return cls

    return decorator


def get_repair_engine(name: str) -> type["RepairEngine"]:
    """Return a registered repair engine class or raise ``KeyError``."""

    normalized = _normalize_engine_name(name)
    try:
        return _REPAIR_ENGINES[normalized]
    except KeyError as exc:
        registered = ", ".join(sorted(_REPAIR_ENGINES)) or "none"
        raise KeyError(f"unknown repair engine: {normalized}. Registered: {registered}") from exc


def get_registry() -> dict[str, type["RepairEngine"]]:
    """Return a copy of the repair engine registry."""

    return dict(_REPAIR_ENGINES)


def list_repair_engines() -> list[str]:
    """Return registered repair engine names in deterministic order."""

    return sorted(_REPAIR_ENGINES)


class RepairEngine(ABC):
    """Base class for verification failure repair engines."""

    engine_name: ClassVar[str]

    @abstractmethod
    def analyze(self, failure: "VerificationFailureReport", dag: "DAG") -> "RootCauseAnalysis":
        """Analyze a verification failure against a DAG snapshot."""

    @abstractmethod
    def propose_fix(self, rca: "RootCauseAnalysis", file_contents: dict[str, str]) -> "RepairProposal":
        """Produce a concrete repair proposal from a root cause analysis."""

    @abstractmethod
    def apply(self, proposal: "RepairProposal", *, dry_run: bool = False) -> "ApplyResult":
        """Apply or preview a repair proposal."""


def _normalize_engine_name(name: str) -> str:
    normalized = str(name).strip()
    if not normalized:
        raise ValueError("repair engine name must be non-empty")
    return normalized


__all__ = [
    "RepairEngine",
    "get_registry",
    "get_repair_engine",
    "list_repair_engines",
    "register_repair_engine",
]
