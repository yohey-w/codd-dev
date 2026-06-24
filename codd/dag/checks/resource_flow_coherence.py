"""DAG check: resource flow coherence.

The data-field sibling of the enablement axis. The enablement axis asks "is a
*capability* that gets exercised also granted/enabled?"; this check asks the
same question for *data resources*: a required capability that CONSUMES a
contract resource cannot function unless some obligation PRODUCES that resource.
A resource that is read by a required capability but written by no obligation is
a "dangling required consumer" — a real incompleteness that must not pass green.
A resource that is written by an obligation but read by no declared consumer is
a "dead resource" — an amber-only ambiguity that must be surfaced without
blocking deploy.

Design notes (kept deliberately conservative to preserve generality and avoid
false reds):

* **Contract-declaration driven.** Producers/consumers come from declared
  ``capability_contracts`` / ``resource_contracts`` in design-doc frontmatter,
  never from scanning implementation source for literals. The core therefore
  knows no project/framework/language tokens; it only reasons over canonical
  resource ids and produce/consume edges.
* **Dormant by default.** A project that declares no resource/capability
  contracts gets ``skip`` (exit code unaffected) — existing projects keep
  passing unchanged.
* **RED only when all hold:** (1) the consumer is required
  (``required: true`` or ``on_missing: fail``), (2) the consuming capability is
  a required capability of a ``critical``/``high`` user journey, and (3) no
  producer / external provider exists for that resource. Anything weaker
  (optional consumer, ``on_missing: skip|degrade``, capability not on a critical
  journey, external/seed provider declared) is not gated.
* **Ordering (opt-in).** When — and only when — a design declares an explicit
  ``operation_flow`` with ordered operations, a *satisfied* required consumer is
  additionally RED (``producer_after_consumer``) if every mapped, non-external
  producer of its resource runs strictly after it (consumed before produced).
  Without an operation_flow, or when the producer/consumer cannot be mapped to a
  single operation (ambiguous), ordering is skipped (no red) — the existence
  check above is unchanged. This keeps ordering reds out of graphs that never
  declared an order, the original reason ordering was deferred.

This is intentionally separate from the enablement axis: ``enables/exercises``
is the *capability* supply relation, ``produces/consumes`` is the *resource*
supply relation. Mixing them muddies severity and diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag.checks import DagCheck, register_dag_check
from codd.requirements_meta import operation_flow_operations


_CRITICAL_LEVELS = {"critical", "high"}
_OPTIONAL_ON_MISSING = {"skip", "degrade", "ignore", "optional", "tolerate"}
_TRUE_TOKENS = {"true", "yes", "1", "required", "must"}
_FALSE_TOKENS = {"false", "no", "0", "optional"}

# Generic ref keys an operation entry may carry to identify itself. Kept
# vocabulary-only (no project/framework/language tokens) so the ordering check
# stays language-free: it matches a producer/consumer's declared capability or
# obligation name against whichever of these keys an operation declares.
_OPERATION_REF_KEYS = (
    "id",
    "name",
    "operation",
    "operation_id",
    "capability",
    "capabilities",
    "obligation",
    "obligations",
)


@dataclass
class ResourceUse:
    resource: str
    direction: str  # "consume" | "produce"
    owner_node_id: str
    capability: str | None = None
    obligation: str | None = None
    required: bool | None = None
    on_missing: str | None = None
    external: bool = False
    operation_index: int | None = None  # LOCAL index within its operation_flow scope
    operation_scope: str | None = None  # the node/flow the local index belongs to
    operation_ref: str | None = None
    # "mapped" | "unmapped" | "ambiguous" — whether this use resolves to exactly
    # one declared operation in the explicit operation_flow ordering.
    operation_mapping_status: str = "unmapped"


@dataclass
class ResourceFlowCoherenceResult:
    check_name: str = "resource_flow_coherence"
    severity: str = "info"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    violations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    passed: bool = True
    skipped: bool = False


@register_dag_check("resource_flow_coherence")
class ResourceFlowCoherenceCheck(DagCheck):
    check_name = "resource_flow_coherence"
    severity = "red"
    block_deploy = True

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> ResourceFlowCoherenceResult:
        target = dag if dag is not None else self.dag
        if target is None:
            raise ValueError("dag is required for resource_flow_coherence check")

        design_docs = [
            node
            for node in sorted(target.nodes.values(), key=lambda item: item.id)
            if getattr(node, "kind", None) == "design_doc"
        ]

        uses: list[ResourceUse] = []
        # alias name -> set of canonical resources it has been declared to target.
        # Collected (not overwritten) so a colliding alias is detectable; a
        # colliding alias is deliberately NOT resolved (kept conservative).
        alias_targets: dict[str, set[str]] = {}
        # Every canonical resource declared by a resource_contract entry.
        canonical_resources: set[str] = set()
        for node in design_docs:
            attrs = getattr(node, "attributes", None) or {}
            uses.extend(self._capability_contract_uses(node.id, attrs))
            node_uses, node_aliases, node_canonicals = self._resource_contract_uses(
                node.id, attrs
            )
            uses.extend(node_uses)
            for alias, target in node_aliases:
                alias_targets.setdefault(alias, set()).add(target)
            canonical_resources.update(node_canonicals)

        # Only aliases that resolve unambiguously (exactly one canonical target)
        # are used for canonicalization. Colliding aliases are left unresolved so
        # the conflicting resource ids stay distinct (no silent collapse).
        alias_map: dict[str, str] = {
            alias: next(iter(targets))
            for alias, targets in alias_targets.items()
            if len(targets) == 1
        }
        # Aliases resolving to >1 canonical are excluded from alias_map above, so a
        # consumer using one is never canonicalized and would look producerless —
        # the dangling check suppresses its red (amber instead) to avoid a false-red.
        ambiguous_aliases = {
            alias for alias, targets in alias_targets.items() if len(targets) > 1
        }

        alias_warnings = self._alias_drift_warnings(alias_targets, canonical_resources)
        malformed_warnings = self._malformed_contract_warnings(design_docs)

        if not uses:
            standalone_warnings = malformed_warnings + alias_warnings
            if standalone_warnings:
                # Contracts were declared but produced no usable produce/consume
                # edges (e.g. every entry malformed, or only alias declarations
                # that collide/shadow). Surface that as amber — never a silent
                # clean skip.
                return ResourceFlowCoherenceResult(
                    severity="amber",
                    status="pass",
                    passed=True,
                    block_deploy=False,
                    message=(
                        f"resource_flow_coherence: {len(standalone_warnings)} declared "
                        "contract issue(s) (malformed/alias) with no usable resource edges"
                    ),
                    warnings=standalone_warnings,
                )
            return ResourceFlowCoherenceResult(
                severity="info",
                status="skip",
                skipped=True,
                passed=True,
                block_deploy=False,
                message="resource_flow_coherence SKIP (no resource/capability contracts declared)",
            )

        critical_caps = self._critical_required_capabilities(design_docs)

        # Explicit operation ordering (opt-in). Only when a design declares an
        # operation_flow with ordered operations can we reason about
        # producer-after-consumer ordering at all; without it we fall back to the
        # existence-based check (no ordering reds — avoids false reds).
        ref_to_index = self._operation_ref_to_index(design_docs)
        self._attach_operation_indices(uses, ref_to_index)

        producers: dict[str, list[ResourceUse]] = {}
        consumers: list[ResourceUse] = []
        for use in uses:
            use.resource = self._canonical(use.resource, alias_map)
            if use.direction == "produce":
                producers.setdefault(use.resource, []).append(use)
            else:
                consumers.append(use)

        violations: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = (
            self._dead_resource_warnings(producers, consumers)
            + malformed_warnings
            + alias_warnings
        )
        for consumer in sorted(consumers, key=lambda c: (c.resource, c.capability or "", c.owner_node_id)):
            if not self._is_required(consumer):
                continue
            if consumer.capability is None or consumer.capability not in critical_caps:
                # Required read, but not tied to a critical/high journey capability.
                # Advisory only: avoids false reds on audit/admin/optional reads.
                warnings.append(
                    {
                        "type": "unscoped_resource_consumer",
                        "severity": "amber",
                        "resource": consumer.resource,
                        "consumer_capability": consumer.capability,
                        "owner_node_id": consumer.owner_node_id,
                        "message": (
                            f"Required consumer reads {consumer.resource} but its capability "
                            "is not a required capability of any critical/high journey; not gated."
                        ),
                        "remediation": (
                            f"Add {consumer.capability} to a critical/high journey's "
                            "required_capabilities to gate it, or mark the consumer "
                            "required:false if it is not essential."
                        ),
                    }
                )
                continue
            if self._has_satisfying_producer(consumer.resource, producers):
                # Producer exists. Existence is satisfied; now — and only when the
                # design declared explicit operation ordering — check that at least
                # one mapped, non-external producer is not strictly after the
                # consumer. A producer-after-consumer ordering bug is a real red.
                order_violation, order_warning = self._producer_order_violation(
                    consumer, producers
                )
                if order_warning is not None:
                    warnings.append(order_warning)
                if order_violation is not None:
                    violations.append(order_violation)
                continue
            if consumer.resource in ambiguous_aliases:
                # Reads an ambiguous alias (>1 canonical target) → left
                # un-canonicalized, so it only looks producerless. Suppress the
                # dangling false-red; surface amber so the author disambiguates.
                warnings.append(
                    {
                        "type": "ambiguous_alias_unresolved",
                        "severity": "amber",
                        "resource": consumer.resource,
                        "consumer_capability": consumer.capability,
                        "owner_node_id": consumer.owner_node_id,
                        "alias_targets": sorted(
                            alias_targets.get(consumer.resource, set())
                        ),
                        "message": (
                            f"Required consumer reads alias {consumer.resource!r}, which "
                            "resolves to multiple canonical resources "
                            f"{sorted(alias_targets.get(consumer.resource, set()))}; it cannot "
                            "be canonicalized, so producer existence is not asserted."
                        ),
                        "remediation": (
                            f"Disambiguate {consumer.resource!r} to one resource (or have the "
                            "consumer name the canonical resource directly)."
                        ),
                    }
                )
                continue
            violations.append(
                {
                    "type": "dangling_required_consumer",
                    "severity": "red",
                    "resource": consumer.resource,
                    "consumer_capability": consumer.capability,
                    "owner_node_id": consumer.owner_node_id,
                    "required_by": "user_journeys[].required_capabilities",
                    "missing": "producer",
                    "message": (
                        f"Required consumer reads {consumer.resource}, but no producer / "
                        "provided-by obligation exists in the contract graph."
                    ),
                    "remediation": (
                        f"Declare a producer / provided-by obligation for {consumer.resource} "
                        "(e.g. a capability that produces it, or externally_provided_by), or "
                        "relax the consumer (required: false / on_missing: skip) if it is not "
                        "truly required."
                    ),
                }
            )

        if violations:
            dangling = sum(
                1 for v in violations if v.get("type") == "dangling_required_consumer"
            )
            after = sum(
                1 for v in violations if v.get("type") == "producer_after_consumer"
            )
            parts: list[str] = []
            if dangling:
                parts.append(f"{dangling} dangling required consumer(s) with no producer obligation")
            if after:
                parts.append(f"{after} producer-after-consumer ordering violation(s)")
            return ResourceFlowCoherenceResult(
                severity="red",
                status="fail",
                passed=False,
                block_deploy=True,
                message="resource_flow_coherence found " + "; ".join(parts),
                violations=violations,
                warnings=warnings,
            )

        # No violations. If amber findings were collected (dead_resource,
        # unscoped_resource_consumer, ambiguous_alias_unresolved, alias drift,
        # malformed_contract, ambiguous_operation_mapping), surface them as
        # amber/warn — the CLI only renders WARN (and counts the finding) when
        # severity == "amber". Returning info/pass here hid those findings behind
        # a green PASS row (a false-green). Deploy stays allowed either way
        # (passed=True, block_deploy=False); with no warnings it is a clean
        # info/pass (unchanged).
        if warnings:
            return ResourceFlowCoherenceResult(
                severity="amber",
                status="warn",
                passed=True,
                block_deploy=False,
                message=(
                    f"resource_flow_coherence found {len(warnings)} advisory "
                    f"warning(s) ({len(consumers)} consumer(s), "
                    f"{len(producers)} producer(s) checked, no violations)"
                ),
                warnings=warnings,
            )

        return ResourceFlowCoherenceResult(
            severity="info",
            status="pass",
            passed=True,
            block_deploy=False,
            message=(
                f"resource_flow_coherence PASS "
                f"({len(consumers)} consumer(s), {len(producers)} producer(s) checked)"
            ),
            warnings=warnings,
        )

    # ------------------------------------------------------------------ collect
    def _capability_contract_uses(self, node_id: str, attrs: dict[str, Any]) -> list[ResourceUse]:
        uses: list[ResourceUse] = []
        for entry in self._entries(attrs, "capability_contracts"):
            capability = self._str(entry.get("capability"))
            for consume in self._dict_list(entry.get("consumes")):
                resource = self._str(consume.get("resource"))
                if not resource:
                    continue
                uses.append(
                    ResourceUse(
                        resource=resource,
                        direction="consume",
                        owner_node_id=node_id,
                        capability=capability,
                        required=self._opt_bool(consume.get("required")),
                        on_missing=self._str(consume.get("on_missing")),
                    )
                )
            for produce in self._dict_list(entry.get("produces")):
                resource = self._str(produce.get("resource"))
                if not resource:
                    continue
                uses.append(
                    ResourceUse(
                        resource=resource,
                        direction="produce",
                        owner_node_id=node_id,
                        capability=capability,
                    )
                )
        return uses

    def _resource_contract_uses(
        self, node_id: str, attrs: dict[str, Any]
    ) -> tuple[list[ResourceUse], list[tuple[str, str]], set[str]]:
        uses: list[ResourceUse] = []
        # (alias, canonical) pairs — collected, not overwritten, so the caller can
        # detect an alias declared against more than one canonical resource.
        aliases: list[tuple[str, str]] = []
        canonicals: set[str] = set()
        for entry in self._entries(attrs, "resource_contracts"):
            resource = self._str(entry.get("resource"))
            if not resource:
                continue
            canonicals.add(resource)
            for alias in self._as_list(entry.get("aliases")):
                alias_s = self._str(alias)
                if alias_s:
                    aliases.append((alias_s, resource))
            for consumer in self._dict_list(entry.get("consumers")):
                uses.append(
                    ResourceUse(
                        resource=resource,
                        direction="consume",
                        owner_node_id=node_id,
                        capability=self._str(consumer.get("capability")),
                        required=self._opt_bool(consumer.get("required")),
                        on_missing=self._str(consumer.get("on_missing")),
                    )
                )
            for producer in self._dict_list(entry.get("producers")):
                uses.append(
                    ResourceUse(
                        resource=resource,
                        direction="produce",
                        owner_node_id=node_id,
                        obligation=self._str(producer.get("obligation") or producer.get("capability")),
                    )
                )
            for ext in self._as_list(entry.get("externally_provided_by")):
                ext_d = ext if isinstance(ext, dict) else {"provider": ext}
                uses.append(
                    ResourceUse(
                        resource=resource,
                        direction="produce",
                        owner_node_id=node_id,
                        obligation=self._str(ext_d.get("provider")),
                        external=True,
                    )
                )
        return uses, aliases, canonicals

    def _critical_required_capabilities(self, design_docs: list[Any]) -> set[str]:
        caps: set[str] = set()
        for node in design_docs:
            attrs = getattr(node, "attributes", None) or {}
            for journey in self._entries(attrs, "user_journeys"):
                level = (self._str(journey.get("criticality")) or "").lower()
                if level not in _CRITICAL_LEVELS:
                    continue
                for cap in self._as_list(journey.get("required_capabilities")):
                    cap_s = self._str(cap)
                    if cap_s:
                        caps.add(cap_s)
        return caps

    # ----------------------------------------------------- operation ordering
    def _operation_ref_to_index(
        self, design_docs: list[Any]
    ) -> dict[str, set[tuple[str, int]]]:
        """Map each operation's declared ref tokens to ``(flow_scope, local_index)``.

        The index is LOCAL to its own ``operation_flow`` (the owning node is the
        flow scope) and resets per design doc — operations are NOT concatenated into
        one global order across docs. Ordering is meaningful only within a single
        explicit flow, so callers compare a consumer and producer only when they
        share a scope; a global index would red independent flows purely by doc-sort
        order (a false-red). A token resolving to more than one (scope, index) is
        ambiguous and treated as unmapped for ordering (no red).
        """

        ref_to_index: dict[str, set[tuple[str, int]]] = {}
        for node in design_docs:
            attrs = getattr(node, "attributes", None) or {}
            scope = str(getattr(node, "id", "") or "")
            for local_index, operation in enumerate(
                operation_flow_operations(attrs.get("operation_flow"))
            ):
                for ref in self._operation_refs(operation):
                    ref_to_index.setdefault(ref, set()).add((scope, local_index))
        return ref_to_index

    def _operation_refs(self, operation: dict[str, Any]) -> set[str]:
        refs: set[str] = set()
        for key in _OPERATION_REF_KEYS:
            for value in self._as_list(operation.get(key)):
                token = self._str(value)
                if token:
                    refs.add(token)
        return refs

    def _attach_operation_indices(
        self, uses: list[ResourceUse], ref_to_index: dict[str, set[tuple[str, int]]]
    ) -> None:
        """Resolve each use's capability/obligation ref to a ``(scope, index)``.

        ``mapped`` = exactly one (scope, index); ``ambiguous`` = more than one;
        otherwise the status stays ``unmapped``. No ref table (no explicit
        operation_flow) leaves every use ``unmapped`` so ordering is skipped.
        """

        for use in uses:
            ref = use.capability or use.obligation
            if not ref:
                continue
            entries = ref_to_index.get(ref)
            if not entries:
                continue
            use.operation_ref = ref
            if len(entries) == 1:
                scope, index = next(iter(entries))
                use.operation_scope = scope
                use.operation_index = index
                use.operation_mapping_status = "mapped"
            else:
                use.operation_mapping_status = "ambiguous"

    def _producer_order_violation(
        self, consumer: ResourceUse, producers: dict[str, list[ResourceUse]]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return ``(violation, warning)`` for one required, satisfied consumer.

        Producer-after-consumer is RED only when ALL of these hold:
          * the consumer is mapped to a single operation index, and
          * the resource has at least one mapped, non-external producer, and
          * every such producer's index is strictly greater than the consumer's.
        Any mapped producer at or before the consumer (``index <= consumer``)
        means the consumer can be satisfied in order → pass (no red). Ambiguous
        consumer/producer mapping yields an amber ``ambiguous_operation_mapping``
        and never a red. Same-index producer/consumer is treated as in-order.
        """

        resource_producers = producers.get(consumer.resource) or []

        if consumer.operation_mapping_status == "ambiguous":
            return None, self._ambiguous_warning(consumer, "consumer")
        if consumer.operation_index is None:
            # No explicit ordering for this consumer → ordering skipped (no red).
            return None, None

        # An external provider seeds the resource outside the operation flow, so
        # it pre-exists any operation by definition. Ordering is moot → skip (no
        # red), even if an internal producer happens to run later.
        if any(producer.external for producer in resource_producers):
            return None, None

        # Only producers in the SAME explicit flow scope as the consumer are
        # comparable. A producer in another doc's independent flow has no ordering
        # relation to this consumer; comparing positions across flows would be a
        # false-red (class: resource_flow_operation_scope_false_red). External
        # providers pre-exist the flow and are out of scope for ordering.
        ordered_producers = [
            producer
            for producer in resource_producers
            if not producer.external
            and producer.operation_index is not None
            and producer.operation_scope == consumer.operation_scope
        ]
        # In order if any same-scope producer runs at or before the consumer. This
        # is checked BEFORE the ambiguous bail so a satisfying mapped producer is
        # not suppressed to amber by an ambiguous sibling.
        if any(
            producer.operation_index <= consumer.operation_index
            for producer in ordered_producers
        ):
            return None, None
        # Not satisfied by a mapped same-scope producer: an ambiguous producer ref
        # means we cannot decide the order → amber (never red).
        if any(
            producer.operation_mapping_status == "ambiguous" and not producer.external
            for producer in resource_producers
        ):
            return None, self._ambiguous_warning(consumer, "producer")
        if not ordered_producers:
            # No mapped same-scope producer to order against → skip (no red).
            return None, None

        producer_indices = sorted(producer.operation_index for producer in ordered_producers)
        violation = {
            "type": "producer_after_consumer",
            "severity": "red",
            "resource": consumer.resource,
            "consumer_capability": consumer.capability,
            "owner_node_id": consumer.owner_node_id,
            "consumer_operation_index": consumer.operation_index,
            "producer_operation_indices": producer_indices,
            "producer_refs": sorted(
                {
                    ref
                    for producer in ordered_producers
                    for ref in [producer.obligation or producer.capability]
                    if ref
                }
            ),
            "required_by": "user_journeys[].required_capabilities",
            "message": (
                f"Required consumer reads {consumer.resource} at operation index "
                f"{consumer.operation_index}, but every mapped producer runs later "
                f"(indices {producer_indices}); the resource is consumed before it is produced."
            ),
            "remediation": (
                f"Reorder operation_flow so a producer of {consumer.resource} precedes its "
                "consumer, add an earlier producer / externally_provided_by source, or relax "
                "the consumer (required: false / on_missing: skip) if the ordering is intended."
            ),
        }
        return violation, None

    @staticmethod
    def _ambiguous_warning(consumer: ResourceUse, role: str) -> dict[str, Any]:
        return {
            "type": "ambiguous_operation_mapping",
            "severity": "amber",
            "resource": consumer.resource,
            "consumer_capability": consumer.capability,
            "owner_node_id": consumer.owner_node_id,
            "role": role,
            "message": (
                f"The {role} for {consumer.resource} resolves to more than one operation in "
                "operation_flow; producer-after-consumer ordering cannot be decided unambiguously, "
                "so it is not gated."
            ),
            "remediation": (
                "Give each operation a unique id/ref so the producer and consumer map to a single "
                "operation, enabling deterministic ordering checks."
            ),
        }

    # --------------------------------------------------------------- predicates
    def _is_required(self, use: ResourceUse) -> bool:
        on_missing = (use.on_missing or "").strip().lower()
        if use.required is False or on_missing in _OPTIONAL_ON_MISSING:
            return False
        if use.required is True or on_missing == "fail":
            return True
        # Neither explicitly required nor fail-on-missing → not gated (conservative).
        return False

    def _has_satisfying_producer(self, resource: str, producers: dict[str, list[ResourceUse]]) -> bool:
        # v1: existence-based. A declared producer / external provider / seed for
        # the same canonical resource satisfies the consumer. Topological ordering
        # is intentionally not enforced here to avoid false reds on graphs without
        # explicit operation flow.
        return bool(producers.get(resource))

    def _dead_resource_warnings(
        self, producers: dict[str, list[ResourceUse]], consumers: list[ResourceUse]
    ) -> list[dict[str, Any]]:
        consumer_resources = {consumer.resource for consumer in consumers}
        warnings: list[dict[str, Any]] = []
        for resource, resource_producers in sorted(producers.items()):
            if resource in consumer_resources:
                continue
            if any(producer.external for producer in resource_producers):
                continue
            producer_refs = sorted(
                {
                    ref
                    for producer in resource_producers
                    for ref in [producer.obligation or producer.capability]
                    if ref
                }
            )
            warnings.append(
                {
                    "type": "dead_resource",
                    "severity": "amber",
                    "resource": resource,
                    "producer_refs": producer_refs,
                    "producer_owner_node_ids": sorted(
                        {producer.owner_node_id for producer in resource_producers}
                    ),
                    "message": (
                        f"Resource {resource} has producer obligation(s) but no declared consumers."
                    ),
                    "remediation": (
                        f"Consume {resource} where it is needed, or drop the producer / mark "
                        "it externally_provided_by if it is intentionally external."
                    ),
                }
            )
        return warnings

    def _alias_drift_warnings(
        self,
        alias_targets: dict[str, set[str]],
        canonical_resources: set[str],
    ) -> list[dict[str, Any]]:
        # Exact-string only (no fuzzy / case-insensitive normalization — same as
        # the rest of this check). Amber only; an alias name conflict is an
        # ambiguity to surface, never a deploy-blocking red.
        warnings: list[dict[str, Any]] = []
        for alias, targets in sorted(alias_targets.items()):
            # (1) duplicate_alias_target — one alias name claims >1 canonical
            # resource. The collision is left unresolved upstream (resources stay
            # distinct); surface it so the ambiguity is not silent.
            if len(targets) > 1:
                warnings.append(
                    {
                        "type": "duplicate_alias_target",
                        "severity": "amber",
                        "alias": alias,
                        "canonical_resources": sorted(targets),
                        "message": (
                            f"Alias '{alias}' is declared against multiple canonical "
                            f"resources ({sorted(targets)}); it is left unresolved so the "
                            "resources stay distinct."
                        ),
                        "remediation": (
                            f"Point alias '{alias}' at a single canonical resource, or give "
                            "each target its own distinct alias."
                        ),
                    }
                )
            # (2) alias_shadows_canonical — an alias name is also a canonical
            # resource id of another entry, so the same token means two things.
            if alias in canonical_resources:
                warnings.append(
                    {
                        "type": "alias_shadows_canonical",
                        "severity": "amber",
                        "alias": alias,
                        "canonical_resources": sorted(targets),
                        "message": (
                            f"Alias '{alias}' is also declared as a canonical resource by "
                            "another contract entry; the same name denotes both an alias and "
                            "a distinct resource."
                        ),
                        "remediation": (
                            f"Rename the alias or the canonical resource so '{alias}' refers "
                            "to exactly one resource."
                        ),
                    }
                )
        return warnings

    def _malformed_contract_warnings(self, design_docs: list[Any]) -> list[dict[str, Any]]:
        # A declared contract entry missing its required key field is unusable: the
        # collectors above drop it silently. Surface it as amber so a malformed
        # declaration is never an invisible no-op.
        warnings: list[dict[str, Any]] = []
        for node in design_docs:
            attrs = getattr(node, "attributes", None) or {}
            for index, entry in enumerate(self._entries(attrs, "capability_contracts")):
                if not self._str(entry.get("capability")):
                    warnings.append(
                        self._malformed(node.id, f"capability_contracts[{index}]", "missing 'capability'")
                    )
                for sub_key in ("consumes", "produces"):
                    for j, sub in enumerate(self._dict_list(entry.get(sub_key))):
                        if not self._str(sub.get("resource")):
                            warnings.append(
                                self._malformed(
                                    node.id,
                                    f"capability_contracts[{index}].{sub_key}[{j}]",
                                    "missing 'resource'",
                                )
                            )
            for index, entry in enumerate(self._entries(attrs, "resource_contracts")):
                if not self._str(entry.get("resource")):
                    warnings.append(
                        self._malformed(node.id, f"resource_contracts[{index}]", "missing 'resource'")
                    )
        return warnings

    @staticmethod
    def _malformed(owner_node_id: str, location: str, detail: str) -> dict[str, Any]:
        return {
            "type": "malformed_contract",
            "severity": "amber",
            "owner_node_id": owner_node_id,
            "location": location,
            "message": (
                f"Declared contract entry at {location} is unusable ({detail}); "
                "it would otherwise be dropped silently."
            ),
            "remediation": f"Fix the entry at {location} ({detail}), or remove it if unintended.",
        }

    # ---------------------------------------------------------- canonical/parse
    def _canonical(self, resource: str, alias_map: dict[str, str]) -> str:
        value = (resource or "").strip()
        return alias_map.get(value, value)

    @staticmethod
    def _entries(attrs: Any, key: str) -> list[dict[str, Any]]:
        """Collect ``key`` entries from a node's attributes.

        Reads the top-level attribute plus the nested ``frontmatter`` and
        ``frontmatter.codd`` locations, mirroring
        ``semantic_contract_conflict._section_entries`` so that contract /
        journey metadata stashed at the canonical ``frontmatter.codd`` position
        (or one level up at ``frontmatter``) by the generator is read, not
        silently skipped (a false-green).

        De-dup of the top-level declaration (avoids double-count). The builder
        lifts a TOP-LEVEL frontmatter ``key`` into BOTH ``attrs[key]`` (the
        extractor's normalized copy) AND keeps the raw copy at
        ``attrs['frontmatter'][key]`` — the *same* logical declaration in two
        places. Reading both would count it twice (verdict unchanged, but
        violation/warning counts doubled). So ``attrs['frontmatter'][key]`` (the
        raw top-level duplicate) is read ONLY when the extractor did not already
        populate ``attrs[key]``; that still covers builder shapes that store only
        the raw frontmatter without lifting it. ``frontmatter.codd[key]`` is a
        genuinely separate location the extractor never lifts to ``attrs[key]``,
        so it is ALWAYS read — preserving the union semantics of the sibling
        check (a top-level decl plus a *different* ``frontmatter.codd`` decl are
        merged, not deduped away).
        """

        if not isinstance(attrs, dict):
            return []
        top_level = attrs.get(key)
        values: list[Any] = [top_level]
        frontmatter = attrs.get("frontmatter")
        if isinstance(frontmatter, dict):
            # Skip the raw top-level duplicate when the extractor already lifted
            # it to attrs[key]; otherwise read it (frontmatter-only builders).
            if not (isinstance(top_level, list) and top_level):
                values.append(frontmatter.get(key))
            codd_meta = frontmatter.get("codd")
            if isinstance(codd_meta, dict):
                values.append(codd_meta.get(key))
        entries: list[dict[str, Any]] = []
        for value in values:
            if isinstance(value, list):
                entries.extend(entry for entry in value if isinstance(entry, dict))
        return entries

    @staticmethod
    def _dict_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [entry for entry in value if isinstance(entry, dict)]

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        return [value]

    @staticmethod
    def _str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _opt_bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        token = str(value).strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        return None
