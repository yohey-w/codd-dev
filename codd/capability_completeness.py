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

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from codd.action_outcome import _normalize_token
from codd.requirements_meta import operation_enables, operation_flow_operations


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

# Verbs that hand an existing target (or access to it) to another actor.
# A grant neither creates the target nor consumes it: it wires *authorization
# reachability* — after the grant, some other actor's operations become
# possible. Kept separate from produce/consume so the existing
# consumer-without-producer computation is untouched.
DEFAULT_GRANT_VERBS: frozenset[str] = frozenset(
    {
        "assign",
        "grant",
        "share",
        "entitle",
        "attach",
        "link",
        "allocate",
        "delegate",
        "authorize",
        "permit",
    }
)

# codd.yaml mapping that tunes this check.
SETTINGS_KEY = "capability_completeness"

# codd.yaml mapping that tunes the enables-declaration doctor nudge.
ENABLES_NUDGE_SETTINGS_KEY = "enables_nudge"

# Operation keys whose values may reference another actor as an observer.
_VISIBILITY_KEYS = (
    "visible_to",
    "visible_for",
    "observer",
    "observers",
    "affected_actor",
    "affected_actors",
)

# Operation keys carrying free-text outcome declarations.
_OUTCOME_TEXT_KEYS = (
    "expected_outcomes",
    "expected_outcome",
    "observable_outcomes",
    "outcomes",
    "outcome",
    "postconditions",
    "postcondition",
)


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


def grant_verb_settings(config: Mapping[str, Any]) -> frozenset[str]:
    """Resolve the grant verb taxonomy from project ``config``.

    Shares ``capability_completeness`` settings: a project may extend the
    built-in grant verbs with ``grant_verbs``. The check's ``enabled`` flag
    governs all advisories emitted by this module.
    """

    settings = config.get(SETTINGS_KEY) if isinstance(config, Mapping) else None
    if not isinstance(settings, Mapping):
        return DEFAULT_GRANT_VERBS
    return _extend_verbs(DEFAULT_GRANT_VERBS, settings.get("grant_verbs"))


def capability_completeness_warnings(
    flows: Iterable[tuple[str, Any]],
    config: Mapping[str, Any],
) -> list[str]:
    """Return advisory warning strings for consumer-without-producer targets
    and grant-without-enables authorization reachability gaps."""

    enabled, produce_verbs, consume_verbs = capability_completeness_settings(config)
    if not enabled:
        return []
    flows = list(flows)
    gaps = detect_capability_gaps(
        flows, produce_verbs=produce_verbs, consume_verbs=consume_verbs
    )
    reachability_gaps = detect_authorization_reachability_gaps(
        flows,
        grant_verbs=grant_verb_settings(config),
        consume_verbs=consume_verbs,
    )
    return [gap.message for gap in gaps] + [gap.message for gap in reachability_gaps]


@dataclass(frozen=True)
class AuthorizationReachabilityGap:
    """A grant operation whose target is consumed elsewhere without `enables` wiring."""

    grant_operation: str
    grant_verb: str
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
            f"[authorization_reachability] Operation '{self.grant_operation}' in {self.source} "
            f"grants target '{self.target}' (verb '{self.grant_verb}') and the target is consumed "
            f"by {consumer_desc}{actor_desc}, but no `enables` declaration wires the grant to the "
            f"consuming operation(s). If the consumers' access depends on this grant, declare "
            f"`enables: [{{actor: ..., operations: [...]}}]` on '{self.grant_operation}' so "
            f"enablement_chain / access_path_variation coverage obligations are derived, or tune "
            f"`grant_verbs` under `{SETTINGS_KEY}` in codd.yaml if this is a false positive."
        )


def _classify_grant_verb(raw_verb: Any, grant_verbs: frozenset[str]) -> bool:
    token = _normalize_token(raw_verb)
    if not token:
        return False
    candidates = [token, *token.split("_")]
    return any(candidate in grant_verbs for candidate in candidates)


def detect_authorization_reachability_gaps(
    flows: Iterable[tuple[str, Any]],
    *,
    grant_verbs: frozenset[str] | None = None,
    consume_verbs: frozenset[str] | None = None,
) -> tuple[AuthorizationReachabilityGap, ...]:
    """Return grant operations whose target is consumed but not `enables`-wired.

    The existing capability check asks "does a produce operation exist?"
    (capability existence). This asks the adjacent question: "the target
    exists, but the consuming actor's access depends on another operation's
    outcome — is that dependency declared?" (authorization reachability).
    Grant operations that already declare ``enables`` are wired and skipped.
    """

    grants = grant_verbs if grant_verbs is not None else DEFAULT_GRANT_VERBS
    consume = consume_verbs if consume_verbs is not None else DEFAULT_CONSUME_VERBS

    gaps: list[AuthorizationReachabilityGap] = []
    for source, flow in flows:
        operations = operation_flow_operations(flow)
        if not operations:
            continue

        consumers: dict[str, list[str]] = {}
        consumer_actors: dict[str, list[str]] = {}
        for index, operation in enumerate(operations):
            target_key = _normalize_token(str(operation.get("target") or "").strip())
            if not target_key:
                continue
            token = _normalize_token(operation.get("verb"))
            candidates = [token, *token.split("_")] if token else []
            if any(candidate in consume for candidate in candidates):
                label = str(operation.get("id") or "").strip() or f"operation[{index}]"
                consumers.setdefault(target_key, []).append(label)
                actor = str(operation.get("actor") or "").strip()
                if actor:
                    consumer_actors.setdefault(target_key, []).append(actor)

        for index, operation in enumerate(operations):
            if not _classify_grant_verb(operation.get("verb"), grants):
                continue
            if operation_enables(operation):
                continue  # already wired
            target_raw = str(operation.get("target") or "").strip()
            target_key = _normalize_token(target_raw)
            if not target_key:
                continue
            grant_label = str(operation.get("id") or "").strip() or f"operation[{index}]"
            consumer_labels = [label for label in consumers.get(target_key, []) if label != grant_label]
            if not consumer_labels:
                continue
            gaps.append(
                AuthorizationReachabilityGap(
                    grant_operation=grant_label,
                    grant_verb=str(operation.get("verb") or "").strip(),
                    target=target_raw or target_key,
                    source=source,
                    consumers=tuple(dict.fromkeys(consumer_labels)),
                    consumer_actors=tuple(dict.fromkeys(consumer_actors.get(target_key, []))),
                )
            )
    return tuple(gaps)


def enablement_declaration_nudges(
    flows: Iterable[tuple[str, Any]],
    config: Mapping[str, Any],
) -> list[str]:
    """Doctor advisory: operations that mention another declared actor in
    ``visible_to``/outcome declarations but declare no ``enables`` relationship.

    This is the entry point for the "declare it or it stays invisible"
    problem: a free-text outcome like "<other actor> can open the shared
    resource" expresses a capability grant, but only a structured ``enables``
    declaration reaches the scenario deriver. The nudge is suppressed when the
    actor name only appears inside a longer word (false-positive control) and
    when the operation already declares ``enables``.
    """

    settings = config.get(ENABLES_NUDGE_SETTINGS_KEY) if isinstance(config, Mapping) else None
    if isinstance(settings, Mapping) and not bool(settings.get("enabled", True)):
        return []

    nudges: list[str] = []
    for source, flow in flows:
        operations = operation_flow_operations(flow)
        if not operations:
            continue
        declared_actors = _declared_actors(flow, operations)
        if len(declared_actors) < 2:
            continue
        for index, operation in enumerate(operations):
            if operation_enables(operation):
                continue
            own_actors = {
                _normalize_token(value)
                for value in _operation_actor_values(operation)
            }
            other_actors = {
                display
                for token, display in declared_actors.items()
                if token and token not in own_actors
            }
            if not other_actors:
                continue
            mentioned = _mentioned_actors(operation, other_actors)
            if not mentioned:
                continue
            label = str(operation.get("id") or "").strip() or f"operation[{index}]"
            actors_desc = ", ".join(f"'{actor}'" for actor in sorted(mentioned))
            nudges.append(
                f"[enables_nudge] Operation '{label}' in {source} mentions actor(s) {actors_desc} "
                f"in visible_to/expected_outcomes but declares no `enables` relationship. If "
                f"completing '{label}' is what authorizes those actor(s) to act (capability, not "
                f"mere observation), declare `enables: [{{actor: ..., operations: [...]}}]` so the "
                f"enablement chain becomes a machine-checkable coverage obligation. Disable via "
                f"`{ENABLES_NUDGE_SETTINGS_KEY}.enabled: false` in codd.yaml."
            )
    return nudges


def _declared_actors(flow: Any, operations: list[dict[str, Any]]) -> dict[str, str]:
    """Map normalized actor token -> display name for all declared actors."""

    actors: dict[str, str] = {}
    candidates: list[str] = []
    if isinstance(flow, Mapping):
        candidates.extend(_text_values(flow.get("actors")))
        candidates.extend(_text_values(flow.get("actor")))
    for operation in operations:
        candidates.extend(_operation_actor_values(operation))
    for candidate in candidates:
        token = _normalize_token(candidate)
        if token:
            actors.setdefault(token, candidate)
    return actors


def _operation_actor_values(operation: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("actor", "actors", "role", "roles"):
        values.extend(_text_values(operation.get(key)))
    return values


def _mentioned_actors(operation: Mapping[str, Any], other_actors: set[str]) -> set[str]:
    """Return other declared actors referenced by visibility keys or outcome text."""

    mentioned: set[str] = set()

    visibility_values = []
    for key in _VISIBILITY_KEYS:
        visibility_values.extend(_text_values(operation.get(key)))
    visibility_tokens = {_normalize_token(value) for value in visibility_values}
    for actor in other_actors:
        if _normalize_token(actor) in visibility_tokens:
            mentioned.add(actor)

    outcome_texts = []
    for key in _OUTCOME_TEXT_KEYS:
        outcome_texts.extend(_text_values(operation.get(key)))
    if outcome_texts:
        haystack = " ".join(outcome_texts)
        for actor in other_actors:
            if _actor_word_match(actor, haystack):
                mentioned.add(actor)
    return mentioned


def _actor_word_match(actor: str, text: str) -> bool:
    """Whole-word actor match; 'user' must not match 'username' (suppression)."""

    parts = [re.escape(part) for part in re.split(r"[\s_]+", actor.strip()) if part]
    if not parts:
        return False
    # Treat underscores/spaces in the actor name interchangeably.
    pattern = r"\b" + r"[\s_]+".join(parts) + r"\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            items.extend(_text_values(item))
        return items
    cleaned = str(value).strip()
    return [cleaned] if cleaned else []
