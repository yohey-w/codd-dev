"""Structured repair result summary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


RepairResultStatus = Literal["SUCCESS", "PARTIAL_SUCCESS", "REPAIR_FAILED", "MAX_ATTEMPTS_REACHED"]


@dataclass
class RepairResult:
    success: bool
    status: RepairResultStatus
    attempts: int = 0
    applied_patches: list[Any] = field(default_factory=list)
    pre_existing_violations: list[Any] = field(default_factory=list)
    unrepairable_violations: list[Any] = field(default_factory=list)
    remaining_violations: list[Any] = field(default_factory=list)
    partial_success_patches: list[Any] = field(default_factory=list)
    reason: str = ""


__all__ = ["RepairResult", "RepairResultStatus"]
