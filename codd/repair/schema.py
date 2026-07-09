"""Serializable schema objects for repair attempts."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    # ── B0 failure attribution (additive; defaults keep old constructors valid) ──
    #: Classification of an executed test/typecheck failure (see
    #: ``codd.repair.test_failure_attribution.FAILURE_CLASSES``). Empty for
    #: structural DAG failures, which are attributed by node, not by class.
    failure_class: str = ""
    #: True when the failure is code-addressable (the repair engine may attempt
    #: a fix) AND was attributed to concrete project files. Drives the
    #: repairability "observed ⇒ current" bypass of the changed-files gate.
    code_addressable: bool = False
    #: Read-only evidence paths (the failing test files) attributed to this
    #: failure — NEVER edit targets. Threaded into the propose prompt as an
    #: IMMUTABLE section (F3) so the engine localizes the bug from the test's
    #: expected-vs-received without ever being handed a test to neuter. Additive;
    #: the default keeps positional constructors valid.
    evidence_nodes: list[str] = field(default_factory=list)


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
    #: F7 (T2) — the LEGAL claim channel for a defective test TRANSCRIPTION. Each
    #: entry is ``{"file", "assertion", "reason"}``: an assertion the engine judges
    #: unsatisfiable by ANY design-conforming implementation (a tautology, or a
    #: contradiction of a design pin / sibling design-pinned assertion). It is NOT a
    #: patch (repair may never edit a test); a CLAIM-ONLY proposal (no ``patches``)
    #: is a STRUCTURED terminal that the loop threads into the outcome so the
    #: greenfield pipeline can re-derive the test from the design. The claim is
    #: NEVER trusted — it is checked by re-derivation + fresh verify. Additive; the
    #: default keeps positional constructors valid.
    test_defect_claim: list[dict] = field(default_factory=list)

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
