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
    "form_submit",
    "dashboard_view",
    "command_button",
    "download_link",
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
        if "enables" in entry:
            # Validation only (warnings for malformed entries); the raw value
            # is kept on the entry so downstream consumers normalize lazily.
            operation_enables(entry, strict=strict, source=f"{source}.operations[{index}]")
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


# Default access path label when an `enables` declaration omits access_paths.
# "granted" = access exists only through the enabling operation's outcome.
DEFAULT_ACCESS_PATHS: tuple[str, ...] = ("granted",)


def operation_enables(
    operation: Any,
    *,
    strict: bool = False,
    source: str = "operation",
) -> list[dict[str, Any]]:
    """Normalize the opt-in ``enables`` declarations on an operation.

    Schema (generic vocabulary only)::

        enables:
          - actor: <actor whose capability is unlocked>
            operations: [<operation ids unlocked by this operation's outcome>]
            access_paths: [granted, direct]   # optional; default [granted]

    Declares that this operation's outcome *enables* another actor to perform
    other operations (capability exercise, not mere observation). Absent or
    empty declarations return ``[]`` so undeclared projects are unaffected.
    Malformed entries are reported and dropped (tolerant parsing, matching
    ``normalize_operation_flow``).
    """

    if not isinstance(operation, Mapping):
        return []
    raw = operation.get("enables")
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, list):
        _warn_or_raise(
            f"{source}.enables must be a list of mappings; ignoring {type(raw).__name__}",
            strict=strict,
        )
        return []

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            _warn_or_raise(f"{source}.enables[{index}] must be a mapping; ignoring entry", strict=strict)
            continue
        actor = str(item.get("actor") or "").strip()
        operations = _string_list(item.get("operations", item.get("operation")))
        if not actor or not operations:
            _warn_or_raise(
                f"{source}.enables[{index}] requires both `actor` and `operations`; ignoring entry",
                strict=strict,
            )
            continue
        access_paths = _string_list(item.get("access_paths", item.get("access_path")))
        if not access_paths:
            access_paths = list(DEFAULT_ACCESS_PATHS)
        entries.append({"actor": actor, "operations": operations, "access_paths": access_paths})
    return entries


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            cleaned = str(item).strip()
            if cleaned:
                items.append(cleaned)
        return items
    cleaned = str(value).strip()
    return [cleaned] if cleaned else []


def _warn_or_raise(message: str, *, strict: bool) -> None:
    if strict:
        raise ValueError(message)
    warnings.warn(message, UserWarning, stacklevel=3)
