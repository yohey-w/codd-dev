"""Generic action outcome coverage helpers."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping


_VERB_ALIASES: dict[str, set[str]] = {
    "create": {"create", "add", "new", "register", "insert"},
    "update": {"update", "edit", "modify", "change", "rename", "patch"},
    "delete": {"delete", "remove", "destroy", "deactivate"},
    "submit": {"submit"},
    "approve": {"approve"},
    "assign": {"assign"},
    "publish": {"publish"},
    "revoke": {"revoke", "unpublish"},
    "import": {"import"},
    "export": {"export"},
    "send": {"send", "notify"},
    "enable": {"enable"},
    "disable": {"disable"},
    "archive": {"archive"},
    "restore": {"restore"},
}
_ALIAS_TO_CANONICAL = {alias: canonical for canonical, aliases in _VERB_ALIASES.items() for alias in aliases}
_AMBIGUOUS_VERBS: dict[str, tuple[str, ...]] = {
    "manage": ("create", "update", "delete"),
    "manage_collection": ("create", "update", "delete"),
    "crud": ("create", "update", "delete"),
}


@dataclass(frozen=True)
class ActionRequirement:
    """A mutating action implied by project operation_flow metadata."""

    source: str
    operation_id: str
    verb: str
    target: str
    actor: str | None = None
    expected_verbs: tuple[str, ...] = ()
    ambiguous: bool = False

    @property
    def display_name(self) -> str:
        bits = [self.operation_id or self.verb]
        if self.target:
            bits.append(f"target={self.target}")
        if self.actor:
            bits.append(f"actor={self.actor}")
        return ", ".join(bits)


@dataclass(frozen=True)
class ActionTargetSpec:
    """An action declared by a runtime action outcome target."""

    target_name: str
    action_id: str
    verb: str | None = None
    target: str | None = None
    outcomes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverageGap:
    requirement: ActionRequirement
    missing_verbs: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class CoverageResult:
    requirements: tuple[ActionRequirement, ...]
    target_actions: tuple[ActionTargetSpec, ...]
    gaps: tuple[CoverageGap, ...]

    @property
    def covered(self) -> bool:
        return not self.gaps


def canonical_action_verb(value: Any) -> str | None:
    """Return a canonical mutating action verb, if *value* names one."""

    raw = _normalize_token(value)
    if not raw:
        return None
    if raw in _AMBIGUOUS_VERBS:
        return raw
    if raw in _ALIAS_TO_CANONICAL:
        return _ALIAS_TO_CANONICAL[raw]
    for part in reversed([part for part in raw.split("_") if part]):
        if part in _ALIAS_TO_CANONICAL:
            return _ALIAS_TO_CANONICAL[part]
    return None


def extract_action_requirements(operation_flow: Any, *, source: str = "operation_flow") -> tuple[ActionRequirement, ...]:
    """Extract mutating action requirements from one operation_flow mapping."""

    if not isinstance(operation_flow, Mapping):
        return ()
    operations = operation_flow.get("operations")
    if not isinstance(operations, list):
        return ()

    requirements: list[ActionRequirement] = []
    for index, raw_operation in enumerate(operations):
        if not isinstance(raw_operation, Mapping):
            continue
        verb = canonical_action_verb(raw_operation.get("verb"))
        if verb is None:
            continue
        operation_id = str(raw_operation.get("id") or f"operation[{index}]")
        target = str(raw_operation.get("target") or "").strip()
        actor = raw_operation.get("actor")
        actor_value = str(actor).strip() if actor not in (None, "") else None
        if verb in _AMBIGUOUS_VERBS:
            requirements.append(
                ActionRequirement(
                    source=source,
                    operation_id=operation_id,
                    verb=verb,
                    target=target,
                    actor=actor_value,
                    expected_verbs=_AMBIGUOUS_VERBS[verb],
                    ambiguous=True,
                )
            )
            continue
        requirements.append(
            ActionRequirement(
                source=source,
                operation_id=operation_id,
                verb=verb,
                target=target,
                actor=actor_value,
                expected_verbs=(verb,),
            )
        )
    return tuple(requirements)


def extract_action_requirements_from_flows(
    flows: Iterable[tuple[str, Any]],
) -> tuple[ActionRequirement, ...]:
    """Extract requirements from ``(source, operation_flow)`` pairs."""

    requirements: list[ActionRequirement] = []
    for source, flow in flows:
        requirements.extend(extract_action_requirements(flow, source=source))
    return tuple(requirements)


def action_target_specs_from_config(config: Mapping[str, Any]) -> tuple[ActionTargetSpec, ...]:
    """Return action metadata declared in ``runtime.action_outcome_targets``."""

    runtime = config.get("runtime")
    if not isinstance(runtime, Mapping):
        return ()
    raw_targets = runtime.get("action_outcome_targets")
    if not isinstance(raw_targets, list):
        return ()

    specs: list[ActionTargetSpec] = []
    for target_index, raw_target in enumerate(raw_targets, start=1):
        if not isinstance(raw_target, Mapping):
            continue
        target_name = str(raw_target.get("name") or f"action outcome {target_index}")
        actions = raw_target.get("actions")
        if actions is None and raw_target.get("action") is not None:
            actions = [raw_target.get("action")]
        if not isinstance(actions, list):
            continue
        default_target = _optional_text(raw_target.get("target"))
        for action_index, raw_action in enumerate(actions, start=1):
            if not isinstance(raw_action, Mapping):
                continue
            action_id = str(raw_action.get("id") or raw_action.get("name") or f"action[{action_index}]")
            verb = canonical_action_verb(raw_action.get("verb")) or canonical_action_verb(action_id)
            specs.append(
                ActionTargetSpec(
                    target_name=target_name,
                    action_id=action_id,
                    verb=verb,
                    target=_optional_text(raw_action.get("target")) or default_target,
                    outcomes=_outcome_names(raw_action.get("outcomes", raw_action.get("outcome"))),
                )
            )
    return tuple(specs)


def compare_action_outcome_coverage(
    requirements: Iterable[ActionRequirement],
    target_actions: Iterable[ActionTargetSpec],
) -> CoverageResult:
    """Compare operation requirements with runtime action outcome metadata."""

    requirement_tuple = tuple(requirements)
    target_tuple = tuple(target_actions)
    gaps: list[CoverageGap] = []
    for requirement in requirement_tuple:
        missing = tuple(
            expected_verb
            for expected_verb in requirement.expected_verbs
            if not _is_covered(requirement, expected_verb, target_tuple)
        )
        if not missing:
            continue
        reason = (
            "ambiguous operation_flow verb requires explicit action outcome metadata"
            if requirement.ambiguous
            else "operation_flow mutating action lacks action outcome metadata"
        )
        gaps.append(CoverageGap(requirement=requirement, missing_verbs=missing, reason=reason))
    return CoverageResult(requirement_tuple, target_tuple, tuple(gaps))


def _is_covered(
    requirement: ActionRequirement,
    expected_verb: str,
    target_actions: tuple[ActionTargetSpec, ...],
) -> bool:
    for target_action in target_actions:
        if _same_token(target_action.action_id, requirement.operation_id):
            return True
        if target_action.verb != expected_verb:
            continue
        if _target_matches(requirement.target, target_action):
            return True
    return False


def _target_matches(required_target: str, target_action: ActionTargetSpec) -> bool:
    required = _normalize_token(required_target)
    if not required:
        return True
    declared = _normalize_token(target_action.target)
    if declared:
        return declared == required
    action_id = _normalize_token(target_action.action_id)
    return bool(
        action_id
        and (
            action_id == required
            or action_id.startswith(f"{required}_")
            or action_id.endswith(f"_{required}")
            or f"_{required}_" in action_id
        )
    )


def _outcome_names(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        return (_normalize_outcome_name(value),)
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                names.append(_normalize_outcome_name(item))
            elif isinstance(item, Mapping):
                raw_name = item.get("name") or item.get("id") or item.get("type")
                if raw_name:
                    names.append(_normalize_outcome_name(raw_name))
        return tuple(name for name in names if name)
    if isinstance(value, Mapping):
        raw_name = value.get("name") or value.get("id") or value.get("type")
        if raw_name:
            return (_normalize_outcome_name(raw_name),)
        return tuple(_normalize_outcome_name(key) for key, enabled in value.items() if bool(enabled))
    return ()


def _same_token(left: str | None, right: str | None) -> bool:
    return bool(_normalize_token(left) and _normalize_token(left) == _normalize_token(right))


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip()


def _normalize_token(value: Any) -> str:
    if value in (None, ""):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _normalize_outcome_name(value: Any) -> str:
    return _normalize_token(value)
