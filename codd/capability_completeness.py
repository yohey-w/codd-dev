"""Actor-capability completeness check for ``operation_flow`` metadata.

Detects *consumer-without-producer* lifecycle gaps: a ``target`` that is
consumed by one or more operations (e.g. ``submit``/``view``/``read``) while no
operation ever produces it (e.g. ``create``/``author``/``update``). Such an
asymmetry usually means an authoring/creation operation was never lifted into
``operation_flow``. Because every operation-driven coverage axis fires *per
declared operation*, an operation that does not exist is structurally invisible:
the project looks green while the actor has no way to bring the resource into
existence.

This is the upstream sibling of the route/state reachability axes
(``navigation_prerequisite``, ``cross_route_state_restore``): those ask whether
a *declared* operation is reachable, while this asks whether the operation
*exists* at all -- capability reachability.

The check is framework-agnostic. It reads only the ``actor``/``verb``/``target``
abstractions of ``operation_flow`` and contains no UI-framework or
project-specific vocabulary. The verb taxonomy is plain English and is
overridable per project via ``codd.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from codd.action_outcome import _normalize_token
from codd.requirements_meta import operation_flow_operations


# Verbs that bring a target into existence or author its content. Having any
# such operation for a target proves the actor can produce it.
DEFAULT_PRODUCE_VERBS: frozenset[str] = frozenset(
    {
        "create",
        "author",
        "add",
        "new",
        "register",
        "insert",
        "generate",
        "write",
        "publish",
        "emit",
        "build",
        "define",
        "draft",
        "compose",
        "make",
        "upload",
        "update",
        "edit",
        "modify",
        "patch",
        "rename",
        "save",
        "store",
    }
)

# Verbs that use, read or reference a target that is assumed to already exist.
DEFAULT_CONSUME_VERBS: frozenset[str] = frozenset(
    {
        "submit",
        "view",
        "read",
        "list",
        "search",
        "download",
        "get",
        "fetch",
        "query",
        "open",
        "complete",
        "finish",
        "browse",
        "display",
        "show",
        "select",
        "render",
        "load",
        "preview",
        "consume",
        "export",
    }
)

# codd.yaml mapping that tunes this check.
SETTINGS_KEY = "capability_completeness"


@dataclass(frozen=True)
class CapabilityGap:
    """A target that is consumed but never produced within an operation_flow."""

    target: str
    source: str
    consumers: tuple[str, ...]
    consumer_actors: tuple[str, ...]

    @property
    def message(self) -> str:
        consumer_desc = ", ".join(self.consumers) if self.consumers else "consume operations"
        actors = [actor for actor in self.consumer_actors if actor]
        actor_desc = f" (actor(s): {', '.join(actors)})" if actors else ""
        return (
            f"[actor_capability_completeness] Target '{self.target}' in {self.source}: "
            f"consumed by {consumer_desc}{actor_desc} but no operation produces it. "
            f"The actor may lack an authoring/create operation to produce this target. "
            f"Add a produce operation (e.g. create/author/update) to operation_flow, "
            f"or tune the produce/consume verb lists under "
            f"`{SETTINGS_KEY}` in codd.yaml if this is a false positive."
        )


def _classify_verb(
    raw_verb: Any,
    produce_verbs: frozenset[str],
    consume_verbs: frozenset[str],
) -> str | None:
    """Classify a verb as ``"produce"``, ``"consume"`` or ``None`` (unknown).

    Produce takes precedence: an operation that both produces and reads a target
    still proves the actor can create it, so the target is not a gap.
    """

    token = _normalize_token(raw_verb)
    if not token:
        return None
    candidates = [token, *token.split("_")]
    if any(candidate in produce_verbs for candidate in candidates):
        return "produce"
    if any(candidate in consume_verbs for candidate in candidates):
        return "consume"
    return None


def _gaps_in_flow(
    source: str,
    flow: Any,
    produce_verbs: frozenset[str],
    consume_verbs: frozenset[str],
) -> list[CapabilityGap]:
    operations = operation_flow_operations(flow)
    if not operations:
        return []

    display: dict[str, str] = {}
    produced: set[str] = set()
    consumers: dict[str, list[str]] = {}
    consumer_actors: dict[str, list[str]] = {}

    for index, operation in enumerate(operations):
        target_raw = str(operation.get("target") or "").strip()
        target_key = _normalize_token(target_raw)
        if not target_key:
            continue
        display.setdefault(target_key, target_raw or target_key)
        kind = _classify_verb(operation.get("verb"), produce_verbs, consume_verbs)
        if kind == "produce":
            produced.add(target_key)
        elif kind == "consume":
            label = str(operation.get("id") or "").strip() or f"operation[{index}]"
            consumers.setdefault(target_key, []).append(label)
            actor = str(operation.get("actor") or "").strip()
            if actor:
                consumer_actors.setdefault(target_key, []).append(actor)

    gaps: list[CapabilityGap] = []
    for target_key, consumer_labels in consumers.items():
        if target_key in produced:
            continue
        gaps.append(
            CapabilityGap(
                target=display.get(target_key, target_key),
                source=source,
                consumers=tuple(dict.fromkeys(consumer_labels)),
                consumer_actors=tuple(dict.fromkeys(consumer_actors.get(target_key, []))),
            )
        )
    return gaps


def detect_capability_gaps(
    flows: Iterable[tuple[str, Any]],
    *,
    produce_verbs: frozenset[str] | None = None,
    consume_verbs: frozenset[str] | None = None,
) -> tuple[CapabilityGap, ...]:
    """Return targets consumed but never produced across ``flows``.

    ``flows`` is an iterable of ``(source, operation_flow)`` pairs, matching the
    shape produced by the CLI's operation-flow collector.
    """

    produce = produce_verbs if produce_verbs is not None else DEFAULT_PRODUCE_VERBS
    consume = consume_verbs if consume_verbs is not None else DEFAULT_CONSUME_VERBS

    gaps: list[CapabilityGap] = []
    for source, flow in flows:
        gaps.extend(_gaps_in_flow(source, flow, produce, consume))
    return tuple(gaps)


def _extend_verbs(base: frozenset[str], extra: Any) -> frozenset[str]:
    if not isinstance(extra, (list, tuple, set)):
        return base
    tokens = {_normalize_token(item) for item in extra}
    tokens.discard("")
    return base | tokens


def capability_completeness_settings(
    config: Mapping[str, Any],
) -> tuple[bool, frozenset[str], frozenset[str]]:
    """Resolve the enable flag and verb taxonomy from project ``config``.

    Defaults: enabled (advisory only) with the built-in verb lists. The settings
    key is absent by default, so existing projects keep the built-in behaviour
    (an opt-in extension that only adds non-fatal advisory warnings). A project
    may opt out with ``enabled: false`` or extend the verb lists with
    ``produce_verbs`` / ``consume_verbs``.
    """

    settings = config.get(SETTINGS_KEY) if isinstance(config, Mapping) else None
    if not isinstance(settings, Mapping):
        return True, DEFAULT_PRODUCE_VERBS, DEFAULT_CONSUME_VERBS

    enabled = bool(settings.get("enabled", True))
    produce = _extend_verbs(DEFAULT_PRODUCE_VERBS, settings.get("produce_verbs"))
    consume = _extend_verbs(DEFAULT_CONSUME_VERBS, settings.get("consume_verbs"))
    return enabled, produce, consume


def capability_completeness_warnings(
    flows: Iterable[tuple[str, Any]],
    config: Mapping[str, Any],
) -> list[str]:
    """Return advisory warning strings for consumer-without-producer targets."""

    enabled, produce_verbs, consume_verbs = capability_completeness_settings(config)
    if not enabled:
        return []
    gaps = detect_capability_gaps(
        flows, produce_verbs=produce_verbs, consume_verbs=consume_verbs
    )
    return [gap.message for gap in gaps]
