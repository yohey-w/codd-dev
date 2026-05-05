"""Approval helpers for repair proposals."""

from __future__ import annotations

import os
import sys
import warnings
from typing import Any, Callable, Literal, Mapping

from codd.repair.schema import RepairProposal


RepairApprovalMode = Literal["required", "auto", "per_attempt"]
APPROVAL_MODES: set[str] = {"required", "auto", "per_attempt"}
DEFAULT_MAX_FILES_PER_AUTO_PROPOSAL = 5

_APPROVE_VALUES = {"1", "approve", "approved", "true", "y", "yes"}
_REJECT_VALUES = {"0", "false", "n", "no", "reject", "rejected", "skip", "skipped"}


class RepairApprovalError(RuntimeError):
    """Raised when a repair proposal cannot pass the approval policy."""


def approve_repair_proposal(
    proposal: RepairProposal,
    *,
    approval_mode: RepairApprovalMode,
    codd_yaml: dict,
    notify_callable: Callable[[str], None] | None = None,
) -> bool:
    """Return whether a repair proposal may be applied."""

    mode = _approval_mode(approval_mode)
    repair_config = _repair_config(codd_yaml)

    if mode == "auto":
        allow_auto = _mapping(repair_config.get("allow_auto"))
        if not bool(allow_auto.get("require_explicit_optin")):
            raise RepairApprovalError(
                "auto repair approval requires repair.allow_auto.require_explicit_optin=true"
            )

        max_files = _max_files_per_auto_proposal(allow_auto)
        if len(proposal.patches) <= max_files:
            return True

        warnings.warn(
            "repair proposal exceeds repair.allow_auto.max_files_per_proposal; "
            "escalating to required approval",
            RuntimeWarning,
            stacklevel=2,
        )
        mode = "required"

    message = _approval_message(proposal, mode)
    if notify_callable is not None:
        notify_callable(message)
    else:
        print(message)

    decision = _configured_decision(repair_config) or os.environ.get("CODD_REPAIR_APPROVAL")
    if decision is not None:
        return _decision_to_bool(decision)

    if sys.stdin.isatty():
        try:
            return _decision_to_bool(input("Approve repair proposal? [y/N]: "))
        except EOFError:
            return False

    return False


def _approval_mode(value: str) -> RepairApprovalMode:
    text = str(value or "required")
    return text if text in APPROVAL_MODES else "required"  # type: ignore[return-value]


def _repair_config(codd_yaml: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return _mapping(codd_yaml.get("repair") if isinstance(codd_yaml, Mapping) else None)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _max_files_per_auto_proposal(allow_auto: Mapping[str, Any]) -> int:
    raw = allow_auto.get("max_files_per_proposal", DEFAULT_MAX_FILES_PER_AUTO_PROPOSAL)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_FILES_PER_AUTO_PROPOSAL


def _configured_decision(repair_config: Mapping[str, Any]) -> Any:
    for key in ("approval_decision", "approval_response"):
        value = repair_config.get(key)
        if value is not None:
            return value
    return None


def _decision_to_bool(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if text in _APPROVE_VALUES:
        return True
    if text in _REJECT_VALUES:
        return False
    raise RepairApprovalError(f"unknown repair approval decision: {value}")


def _approval_message(proposal: RepairProposal, mode: RepairApprovalMode) -> str:
    files = ", ".join(patch.file_path for patch in proposal.patches) or "none"
    return (
        "CoDD repair approval required "
        f"(mode={mode}, patches={len(proposal.patches)}, files={files})"
    )


__all__ = [
    "APPROVAL_MODES",
    "DEFAULT_MAX_FILES_PER_AUTO_PROPOSAL",
    "RepairApprovalError",
    "RepairApprovalMode",
    "approve_repair_proposal",
]
