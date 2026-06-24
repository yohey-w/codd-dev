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

This is intentionally separate from the enablement axis: ``enables/exercises``
is the *capability* supply relation, ``produces/consumes`` is the *resource*
supply relation. Mixing them muddies severity and diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag.checks import DagCheck, register_dag_check


_CRITICAL_LEVELS = {"critical", "high"}
_OPTIONAL_ON_MISSING = {"skip", "degrade", "ignore", "optional", "tolerate"}
_TRUE_TOKENS = {"true", "yes", "1", "required", "must"}
_FALSE_TOKENS = {"false", "no", "0", "optional"}


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
        alias_map: dict[str, str] = {}
        for node in design_docs:
            attrs = getattr(node, "attributes", None) or {}
            uses.extend(self._capability_contract_uses(node.id, attrs))
            node_uses, node_aliases = self._resource_contract_uses(node.id, attrs)
            uses.extend(node_uses)
            alias_map.update(node_aliases)

        malformed_warnings = self._malformed_contract_warnings(design_docs)

        if not uses:
            if malformed_warnings:
                # Contracts were declared but every entry is unusable (missing a
                # required field). Surface that as amber — never a silent clean skip.
                return ResourceFlowCoherenceResult(
                    severity="amber",
                    status="pass",
                    passed=True,
                    block_deploy=False,
                    message=(
                        f"resource_flow_coherence: {len(malformed_warnings)} declared "
                        "contract entry(ies) unusable (malformed)"
                    ),
                    warnings=malformed_warnings,
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
            self._dead_resource_warnings(producers, consumers) + malformed_warnings
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
                    }
                )
                continue
            if self._has_satisfying_producer(consumer.resource, producers):
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
                }
            )

        if violations:
            return ResourceFlowCoherenceResult(
                severity="red",
                status="fail",
                passed=False,
                block_deploy=True,
                message=(
                    f"resource_flow_coherence found {len(violations)} dangling required "
                    "consumer(s) with no producer obligation"
                ),
                violations=violations,
                warnings=warnings,
            )

        return ResourceFlowCoherenceResult(
            severity="info",
            status="pass",
            passed=True,
            block_deploy=False,
            message="resource_flow_coherence PASS",
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
    ) -> tuple[list[ResourceUse], dict[str, str]]:
        uses: list[ResourceUse] = []
        aliases: dict[str, str] = {}
        for entry in self._entries(attrs, "resource_contracts"):
            resource = self._str(entry.get("resource"))
            if not resource:
                continue
            for alias in self._as_list(entry.get("aliases")):
                alias_s = self._str(alias)
                if alias_s:
                    aliases[alias_s] = resource
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
        return uses, aliases

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
        }

    # ---------------------------------------------------------- canonical/parse
    def _canonical(self, resource: str, alias_map: dict[str, str]) -> str:
        value = (resource or "").strip()
        return alias_map.get(value, value)

    @staticmethod
    def _entries(attrs: Any, key: str) -> list[dict[str, Any]]:
        value = attrs.get(key, []) if isinstance(attrs, dict) else []
        if not isinstance(value, list):
            return []
        return [entry for entry in value if isinstance(entry, dict)]

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
