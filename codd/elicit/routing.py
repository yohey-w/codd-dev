"""Stage-2 Axis-P Phase C1: gap_kind -> contract_key routing table.

A ``CONFIRMED`` coverage decision (Phase A produced an owner-free amber
``AskItem``; a human batch-confirmed it) is promotable to an *explicit*
contract so the existing deterministic checks (resource_flow_coherence,
negative_space, user_journey_coherence, ...) can enforce it and go red on a
violation. This module owns the *only* piece of new meaning in that promotion:
**which contract key a given gap kind becomes**.

Design (rails):

* **RECOMMENDED default, owner-overridable.** :data:`DEFAULT_GAP_ROUTING` is a
  recommendation, never a hard-coded law. A project overrides it under
  ``codd.yaml`` ``axis_p.gap_routing`` (or a caller passes an explicit mapping).
  The default is the *recommendation* the design proposes; the owner stays in
  control of the gap_kind -> contract mapping.
* **Unknown kind -> no routing (safe side).** A gap kind that matches no rule
  resolves to an empty target list. The promoter then leaves the decision as
  amber residue rather than inventing a contract for a meaning the table does
  not recognize. New meaning is never hard-coded into a contract silently.
* **Generality.** Patterns and contract keys are CoDD-vocabulary tokens only
  (``user_journeys``, ``resource_contracts`` ...). Nothing here branches on a
  project / framework / programming-language literal.
* **Deterministic.** Matching is pure string/glob; the same gap kind always
  routes to the same ordered target keys.

Pattern syntax is a deliberately tiny glob: a trailing ``*`` is a prefix match
(``missing_journey*`` matches ``missing_journey_for_actor``); otherwise the rule
matches the kind exactly. No regex, no domain literals — the table stays
readable and override-friendly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


#: CoDD config namespace + key under which a project overrides the routing table.
#: ``codd.yaml`` shape (RECOMMENDED default is merged, project wins per key)::
#:
#:     axis_p:
#:       gap_routing:
#:         missing_journey*: [user_journeys]
#:         my_custom_gap_kind: [resource_contracts]
CONFIG_NAMESPACE = "axis_p"
CONFIG_ROUTING_KEY = "gap_routing"

#: Marker recorded on every promoted contract entry so the promotion is
#: traceable and idempotent (re-promotion recognizes its own prior output).
PROMOTION_SOURCE = "axis_p_confirmed"

# The RECOMMENDED default routing table. Ordered: the first rule whose pattern
# matches the gap kind wins (so put more specific prefixes before broader ones).
# Each value is the ordered list of contract keys the gap promotes into. These
# are the canonical frontmatter.codd contract keys the deterministic checks read.
#
# NB this is a *recommendation*, not a law — see module docstring. A project can
# replace any rule (or the whole table) via codd.yaml axis_p.gap_routing.
DEFAULT_GAP_ROUTING: tuple[tuple[str, tuple[str, ...]], ...] = (
    # journeys / actors with no declared journey -> user_journeys
    ("missing_journey*", ("user_journeys",)),
    # a resource consumed but produced by nobody -> resource + capability flow
    ("missing_producer*", ("resource_contracts", "capability_contracts")),
    ("consumed_not_produced*", ("resource_contracts", "capability_contracts")),
    # environment / variant coverage gaps -> coverage_axes
    ("environment*", ("coverage_axes",)),
    ("variant*", ("coverage_axes",)),
    # forbidden / negative-space gaps -> negative_space.forbidden_evidence
    ("forbidden*", ("negative_space.forbidden_evidence",)),
    ("negative_space*", ("negative_space.forbidden_evidence",)),
    # cardinality / aggregation gaps -> aggregation_policies
    ("cardinality*", ("aggregation_policies",)),
    # acceptance signal / e2e outcome gaps -> a journey's expected_outcomes
    ("acceptance_signal*", ("user_journeys.expected_outcomes",)),
    ("e2e*", ("user_journeys.expected_outcomes",)),
    # non-functional requirement gaps -> runtime_constraints
    ("nfr*", ("runtime_constraints",)),
)


def resolve_routing(
    codd_config: Mapping[str, Any] | None = None,
    *,
    override: Mapping[str, Any] | None = None,
) -> list[tuple[str, tuple[str, ...]]]:
    """Return the effective routing rules (RECOMMENDED default + overrides).

    Resolution order (later wins *per pattern*):

    1. :data:`DEFAULT_GAP_ROUTING` — the recommendation.
    2. ``codd_config[axis_p][gap_routing]`` — the project's ``codd.yaml`` override.
    3. ``override`` — an explicit per-call mapping (highest precedence; used by
       callers / tests that pass a routing directly).

    An override entry whose pattern already exists in the default *replaces* that
    rule's targets (the owner re-points an existing gap kind); a new pattern is
    *appended* (the owner routes a custom gap kind). Targets are normalized to a
    tuple of non-empty contract-key strings; an override mapping a pattern to an
    empty list explicitly disables routing for that kind (it then yields no
    targets -> the promoter leaves it as amber residue).

    The result preserves default order first (with replaced targets in place),
    then appended override patterns in input order — deterministic.
    """
    rules: list[tuple[str, tuple[str, ...]]] = [
        (pattern, targets) for pattern, targets in DEFAULT_GAP_ROUTING
    ]

    merged_overrides: dict[str, tuple[str, ...]] = {}
    for source in (_routing_from_config(codd_config), _normalize_routing(override)):
        merged_overrides.update(source)

    if not merged_overrides:
        return rules

    index_by_pattern = {pattern: i for i, (pattern, _) in enumerate(rules)}
    for pattern, targets in merged_overrides.items():
        if pattern in index_by_pattern:
            rules[index_by_pattern[pattern]] = (pattern, targets)
        else:
            rules.append((pattern, targets))
            index_by_pattern[pattern] = len(rules) - 1
    return rules


def route_gap_kind(
    gap_kind: str | None,
    rules: Sequence[tuple[str, tuple[str, ...]]] | None = None,
    *,
    codd_config: Mapping[str, Any] | None = None,
    override: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return the contract keys a ``gap_kind`` promotes into, or ``()``.

    ``rules`` (already resolved via :func:`resolve_routing`) is used when
    provided; otherwise the rules are resolved from ``codd_config`` / ``override``
    here. An empty / unknown kind, or a kind matching no rule, returns ``()`` —
    the safe side: the caller must NOT promote it (no new meaning invented).
    """
    kind = _canonical_kind(gap_kind)
    if not kind:
        return ()
    effective = (
        list(rules)
        if rules is not None
        else resolve_routing(codd_config, override=override)
    )
    for pattern, targets in effective:
        if _pattern_matches(pattern, kind):
            return targets
    return ()


def split_contract_key(contract_key: str) -> tuple[str, str | None]:
    """Split a routed contract key into ``(top_key, sub_key)``.

    A dotted key such as ``user_journeys.expected_outcomes`` targets a field
    *inside* each ``user_journeys`` entry; ``negative_space.forbidden_evidence``
    targets the ``forbidden_evidence`` list under the ``negative_space`` mapping.
    A plain key (``resource_contracts``) has ``sub_key = None`` and targets a
    top-level list. Only the first ``.`` is split (keys are shallow by design).
    """
    key = (contract_key or "").strip()
    if "." in key:
        top, sub = key.split(".", 1)
        return top.strip(), (sub.strip() or None)
    return key, None


# ---------------------------------------------------------------------------
# internals (pure, deterministic)
# ---------------------------------------------------------------------------

def _routing_from_config(
    codd_config: Mapping[str, Any] | None,
) -> dict[str, tuple[str, ...]]:
    if not isinstance(codd_config, Mapping):
        return {}
    namespace = codd_config.get(CONFIG_NAMESPACE)
    if not isinstance(namespace, Mapping):
        return {}
    return _normalize_routing(namespace.get(CONFIG_ROUTING_KEY))


def _normalize_routing(raw: Any) -> dict[str, tuple[str, ...]]:
    """Coerce an override mapping into ``{pattern: (contract_key, ...)}``.

    Accepts a mapping of pattern -> (str | list[str]). Non-mapping input, empty
    patterns, and non-string targets are dropped. An empty target list is kept
    (an explicit "disable this kind"): it normalizes to ``()``.
    """
    if not isinstance(raw, Mapping):
        return {}
    normalized: dict[str, tuple[str, ...]] = {}
    for pattern, targets in raw.items():
        pattern_s = str(pattern).strip()
        if not pattern_s:
            continue
        normalized[pattern_s] = _normalize_targets(targets)
    return normalized


def _normalize_targets(targets: Any) -> tuple[str, ...]:
    if targets is None:
        return ()
    if isinstance(targets, str):
        candidates: list[Any] = [targets]
    elif isinstance(targets, (list, tuple)):
        candidates = list(targets)
    else:
        return ()
    out: list[str] = []
    for candidate in candidates:
        token = str(candidate).strip()
        if token and token not in out:
            out.append(token)
    return tuple(out)


def _canonical_kind(value: Any) -> str:
    """Canonicalize a gap kind the same way the AskItem id token is built.

    Lower-cased, non-alphanumerics collapsed to ``_`` (matches
    ``gap_to_ask._canonical_token``) so routing a kind recovered from a parsed
    AskItem id matches a kind read from a structured field identically.
    """
    if not isinstance(value, str):
        if value is None:
            return ""
        value = str(value)
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _pattern_matches(pattern: str, kind: str) -> bool:
    pattern = (pattern or "").strip()
    if not pattern:
        return False
    canonical_pattern = _canonical_kind(pattern.rstrip("*"))
    if pattern.endswith("*"):
        # Trailing ``*`` is a prefix glob on the canonical token: ``missing_journey*``
        # matches ``missing_journey_for_actor``. An empty prefix (``*``) would match
        # everything; guard against it (a catch-all rule must be explicit, not a typo).
        if not canonical_pattern:
            return False
        return kind.startswith(canonical_pattern)
    return kind == canonical_pattern


__all__ = [
    "CONFIG_NAMESPACE",
    "CONFIG_ROUTING_KEY",
    "DEFAULT_GAP_ROUTING",
    "PROMOTION_SOURCE",
    "resolve_routing",
    "route_gap_kind",
    "split_contract_key",
]
