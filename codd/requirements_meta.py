"""Helpers for optional requirement metadata consumed by CoDD prompts/checks."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping
import warnings


ALLOWED_UI_PATTERNS = {
    "single_form",
    "list",
    "list_detail",
    "master_detail",
    "drilldown",
    "wizard",
    "modal",
    "inline_edit",
}


def normalize_operation_flow(
    value: Any,
    *,
    strict: bool = False,
    source: str = "operation_flow",
) -> dict[str, Any] | None:
    """Return a tolerant copy of ``operation_flow`` metadata.

    Unknown ``ui_pattern`` values are reported but kept. The field is an LLM
    steering hint, so loose parsing preserves author intent and avoids breaking
    existing documents.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        _warn_or_raise(f"{source} must be a mapping; ignoring {type(value).__name__}", strict=strict)
        return None

    normalized = deepcopy(dict(value))
    operations = normalized.get("operations")
    if operations is None:
        normalized["operations"] = []
        return normalized
    if not isinstance(operations, list):
        _warn_or_raise(f"{source}.operations must be a list; ignoring {type(operations).__name__}", strict=strict)
        normalized["operations"] = []
        return normalized

    kept: list[dict[str, Any]] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            _warn_or_raise(f"{source}.operations[{index}] must be a mapping; ignoring entry", strict=strict)
            continue
        entry = deepcopy(dict(operation))
        ui_pattern = str(entry.get("ui_pattern") or "").strip()
        if ui_pattern and ui_pattern not in ALLOWED_UI_PATTERNS:
            _warn_or_raise(
                f"unknown operation_flow ui_pattern {ui_pattern!r} at {source}.operations[{index}]",
                strict=strict,
            )
        kept.append(entry)
    normalized["operations"] = kept
    return normalized


def operation_flow_operations(value: Any) -> list[dict[str, Any]]:
    """Return operation mappings from a tolerant ``operation_flow`` payload."""

    if not isinstance(value, Mapping):
        return []
    operations = value.get("operations")
    if not isinstance(operations, list):
        return []
    return [dict(item) for item in operations if isinstance(item, Mapping)]


def _warn_or_raise(message: str, *, strict: bool) -> None:
    if strict:
        raise ValueError(message)
    warnings.warn(message, UserWarning, stacklevel=3)
