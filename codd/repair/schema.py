"""Serializable schema objects for repair attempts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PatchMode = Literal["unified_diff", "full_file_replacement"]

PATCH_MODES: set[str] = {"unified_diff", "full_file_replacement"}


@dataclass
class VerificationFailureReport:
    check_name: str
    failed_nodes: list[str]
    error_messages: list[str]
    dag_snapshot: dict
    timestamp: str


@dataclass
class RootCauseAnalysis:
    probable_cause: str
    affected_nodes: list[str]
    repair_strategy: PatchMode
    confidence: float
    analysis_timestamp: str

    def __post_init__(self) -> None:
        _validate_patch_mode(self.repair_strategy)
        _validate_confidence(self.confidence)


@dataclass
class FilePatch:
    file_path: str
    patch_mode: PatchMode
    content: str

    def __post_init__(self) -> None:
        _validate_patch_mode(self.patch_mode)


@dataclass
class RepairProposal:
    patches: list[FilePatch]
    rationale: str
    confidence: float
    proposal_timestamp: str
    rca_reference: str

    def __post_init__(self) -> None:
        self.patches = [patch if isinstance(patch, FilePatch) else FilePatch(**patch) for patch in self.patches]
        _validate_confidence(self.confidence)


@dataclass
class ApplyResult:
    success: bool
    applied_patches: list[str]
    failed_patches: list[str]
    error_message: str | None


def _validate_patch_mode(value: str) -> None:
    if value not in PATCH_MODES:
        raise ValueError(f"patch_mode must be one of {sorted(PATCH_MODES)}")


def _validate_confidence(value: float) -> None:
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")


__all__ = [
    "ApplyResult",
    "FilePatch",
    "PATCH_MODES",
    "PatchMode",
    "RepairProposal",
    "RootCauseAnalysis",
    "VerificationFailureReport",
]
