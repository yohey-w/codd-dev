"""Deterministic IaC-resource → non-functional-requirement (NFR) mapping.

Application source code yields *functional* structure; it cannot tell you the
operations/infrastructure layer or the non-functional requirements a system was
built to satisfy. Those live in Infrastructure-as-Code (IaC): replica counts,
autoscaling bounds, resource limits, health probes, network policies, backup
retention, multi-AZ, CI/CD pipelines, and so on.

This module is the keystone of brownfield *reverse-restoration* of that layer.
It is **pure and deterministic** (no LLM, no I/O): given the structured IaC
facts already produced by :mod:`codd.parsing` (the ``infra_config`` map of
:class:`~codd.parsing.ConfigInfo`), it emits a list of
:class:`NfrCandidate` records. Each carries:

* ``category``   — one of :data:`CATEGORIES` (availability, scalability,
  performance/capacity, reliability/health, security/isolation, durability/DR,
  observability/SLO, deployment_topology).
* ``statement``  — a human-readable, prescriptive NFR or operational fact, e.g.
  *"Service 'api' runs >=3 replicas -> availability target: tolerate
  single-instance failure"*.
* ``source``     — provenance: the IaC file plus the specific resource it was
  derived from, so every inferred requirement is auditable.
* ``confidence`` — ``"high"`` for direct facts (an explicit replica count, an
  explicit backup-retention value) or ``"medium"`` for inferred intent (the mere
  *presence* of monitoring rules implying an SLO source).
* ``kind``       — ``"nfr"`` (a prescriptive non-functional requirement) or
  ``"operational_fact"`` (a recovered ops/topology fact that is evidence, not
  itself a requirement).

The mapping table is encoded explicitly below (see :func:`derive_iac_nfrs` and
the per-format helpers). It is vendor/stack-neutral: Kubernetes, Terraform,
Docker, GitHub Actions, Ansible and Prometheus are general tools, named
directly; no specific company's resources are hardcoded.

Deep parse vs. recognition: Ansible plays/tasks and Prometheus rule files are
deep-parsed (``format == "ansible"`` / ``"prometheus"``) and map to specific,
mostly HIGH-confidence candidates; files that could not be deep-parsed still
arrive as ``format == "ops-evidence"`` and keep the original recognition-only
MEDIUM mappings as the fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from codd.confidence import (
    CATEGORY_MEDIUM,
    band_for,
    band_for_fact,
    numeric_for_category,
)


# ---------------------------------------------------------------------------
# Categories & confidence
# ---------------------------------------------------------------------------
CAT_AVAILABILITY = "availability"
CAT_SCALABILITY = "scalability"
CAT_CAPACITY = "performance/capacity"
CAT_RELIABILITY = "reliability/health"
CAT_SECURITY = "security/isolation"
CAT_DURABILITY = "durability/DR"
CAT_OBSERVABILITY = "observability/SLO"
CAT_TOPOLOGY = "deployment_topology"

CATEGORIES: tuple[str, ...] = (
    CAT_AVAILABILITY,
    CAT_SCALABILITY,
    CAT_CAPACITY,
    CAT_RELIABILITY,
    CAT_SECURITY,
    CAT_DURABILITY,
    CAT_OBSERVABILITY,
    CAT_TOPOLOGY,
)

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"

KIND_NFR = "nfr"
KIND_OPERATIONAL_FACT = "operational_fact"

# Substrings (lowercased) of Terraform resource TYPES that imply isolation/IAM
# intent. Provider-neutral tokens, matched as substrings.
_SECURITY_TYPE_TOKENS: tuple[str, ...] = (
    "security_group",
    "firewall",
    "iam_",
    "network_acl",
    "waf",
    "kms_",
)


@dataclass(frozen=True)
class NfrCandidate:
    """A single inferred NFR candidate or operational fact with provenance."""

    category: str
    statement: str
    source: str
    confidence: str
    kind: str = KIND_NFR
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "statement": self.statement,
            "source": self.source,
            "confidence": self.confidence,
            "kind": self.kind,
            "evidence": dict(self.evidence),
        }

    # -- canonical-confidence bridges (additive; `confidence` strings remain
    #    the stored/serialized vocabulary for backward compatibility) --------
    @property
    def numeric_confidence(self) -> float:
        """The candidate's confidence on the canonical 0.0–1.0 scale.

        Bridged via :func:`codd.confidence.numeric_for_category`
        (``high`` ⇒ 0.95, ``medium`` ⇒ 0.60). An unrecognized category is
        treated as ``medium`` — the same "never over-state" lenience the
        restoration report applies to unknown bands.
        """

        return numeric_for_category(
            self.confidence,
            default=numeric_for_category(CATEGORY_MEDIUM),
        )

    @property
    def band(self) -> str:
        """Derived confidence band (``green`` / ``amber`` / ``gray``).

        Single-source decision (deliberate): a ``high`` candidate is a *direct
        IaC fact* — a value deterministically parsed from its one authoritative
        declaration (an explicit replica count, an explicit backup-retention
        value). The bands-config ``min_evidence_count`` rule exists to demand
        corroboration of INFERRED statements; a declaration read verbatim has
        nothing to corroborate — it IS the source of truth. So ``high`` +
        single-source classifies as green (:func:`codd.confidence.band_for_fact`).
        ``medium`` candidates ARE inferred intent (e.g. monitoring rules
        implying an SLO source), so they go through the standard
        single-evidence rule (:func:`codd.confidence.band_for`) ⇒ amber.
        """

        if self.confidence == CONFIDENCE_HIGH:
            return band_for_fact(self.numeric_confidence)
        return band_for(self.numeric_confidence, evidence_count=1)


def _source(file_path: str, *parts: str) -> str:
    """Build a stable provenance string: ``file::resource``."""

    suffix = "::".join(p for p in parts if p)
    return f"{file_path}::{suffix}" if suffix else file_path


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "enabled", "on"}
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def derive_iac_nfrs(
    infra_config: Mapping[str, Any] | Iterable[Any],
) -> list[NfrCandidate]:
    """Derive NFR candidates + operational facts from structured IaC facts.

    ``infra_config`` is either the :class:`~codd.extractor.ProjectFacts`
    ``infra_config`` mapping (``{relative_path: ConfigInfo}``) or any iterable of
    ``ConfigInfo``-like objects (each must expose ``format``, ``file_path``,
    ``services``, ``resources`` and the optional ``pipelines``/``images``/
    ``recognized_kind`` fields). Output ordering is deterministic: by source file
    then by the encoded mapping order.

    Pure: no I/O, no LLM, no mutation of inputs.
    """

    configs = list(infra_config.values()) if isinstance(infra_config, Mapping) else list(infra_config)

    candidates: list[NfrCandidate] = []
    # Aggregate signals that need a cross-resource view (deployment topology).
    namespaces: set[str] = set()
    gha_environments: set[str] = set()

    for config in sorted(configs, key=lambda c: getattr(c, "file_path", "")):
        fmt = getattr(config, "format", "")
        if fmt == "kubernetes":
            candidates.extend(_map_kubernetes(config, namespaces))
        elif fmt == "terraform":
            candidates.extend(_map_terraform(config))
        elif fmt == "docker-compose":
            candidates.extend(_map_docker_compose(config))
        elif fmt == "github-actions":
            candidates.extend(_map_github_actions(config, gha_environments))
        elif fmt == "dockerfile":
            candidates.extend(_map_dockerfile(config))
        elif fmt == "ansible":
            candidates.extend(_map_ansible(config))
        elif fmt == "prometheus":
            candidates.extend(_map_prometheus(config))
        elif fmt == "ops-evidence":
            candidates.extend(_map_ops_evidence(config))

    candidates.extend(_map_topology(namespaces, gha_environments))
    return candidates


# ---------------------------------------------------------------------------
# Kubernetes mapping
# ---------------------------------------------------------------------------
def _map_kubernetes(config: Any, namespaces: set[str]) -> list[NfrCandidate]:
    out: list[NfrCandidate] = []
    file_path = getattr(config, "file_path", "")

    for resource in getattr(config, "resources", []) or []:
        kind = resource.get("kind")
        name = resource.get("name") or ""
        namespace = resource.get("namespace")
        if namespace:
            namespaces.add(str(namespace))

        if kind in {"Deployment", "StatefulSet"}:
            out.extend(_k8s_workload(file_path, resource))
        elif kind in {"DaemonSet", "Job", "CronJob"}:
            out.extend(_k8s_other_workload(file_path, resource))
        elif kind == "HorizontalPodAutoscaler":
            out.extend(_k8s_hpa(file_path, resource))
        elif kind == "NetworkPolicy":
            out.append(
                NfrCandidate(
                    category=CAT_SECURITY,
                    statement=(
                        f"NetworkPolicy '{name}' restricts pod traffic "
                        f"(policyTypes={resource.get('policy_types') or []}) -> "
                        "security/isolation: enforce network segmentation between workloads"
                    ),
                    source=_source(file_path, "NetworkPolicy", name),
                    confidence=CONFIDENCE_HIGH,
                    evidence={
                        "policy_types": resource.get("policy_types"),
                        "ingress_rules": resource.get("ingress_rules"),
                        "egress_rules": resource.get("egress_rules"),
                    },
                )
            )
        elif kind == "PodDisruptionBudget":
            target = resource.get("min_available")
            metric = "minAvailable" if target is not None else "maxUnavailable"
            value = target if target is not None else resource.get("max_unavailable")
            out.append(
                NfrCandidate(
                    category=CAT_AVAILABILITY,
                    statement=(
                        f"PodDisruptionBudget '{name}' guarantees {metric}={value} "
                        "-> availability: bound voluntary-disruption impact during maintenance"
                    ),
                    source=_source(file_path, "PodDisruptionBudget", name),
                    confidence=CONFIDENCE_HIGH,
                    evidence={metric: value},
                )
            )
        elif kind == "PersistentVolumeClaim":
            out.append(
                NfrCandidate(
                    category=CAT_DURABILITY,
                    statement=(
                        f"PersistentVolumeClaim '{name}' requests durable storage "
                        f"(storage={resource.get('storage')}, "
                        f"access_modes={resource.get('access_modes') or []}) -> "
                        "durability/DR: persistent state must survive pod rescheduling"
                    ),
                    source=_source(file_path, "PersistentVolumeClaim", name),
                    confidence=CONFIDENCE_HIGH,
                    evidence={
                        "storage": resource.get("storage"),
                        "access_modes": resource.get("access_modes"),
                        "storage_class": resource.get("storage_class"),
                    },
                )
            )

    return out


def _k8s_workload(file_path: str, resource: Any) -> list[NfrCandidate]:
    out: list[NfrCandidate] = []
    kind = resource.get("kind")
    name = resource.get("name") or ""
    replicas = _as_int(resource.get("replicas"))

    if replicas is not None:
        if replicas >= 2:
            out.append(
                NfrCandidate(
                    category=CAT_AVAILABILITY,
                    statement=(
                        f"{kind} '{name}' runs >={replicas} replicas -> availability "
                        "target: tolerate single-instance failure (no single point of failure)"
                    ),
                    source=_source(file_path, kind, name),
                    confidence=CONFIDENCE_HIGH,
                    evidence={"replicas": replicas},
                )
            )
            out.append(
                NfrCandidate(
                    category=CAT_SCALABILITY,
                    statement=(
                        f"{kind} '{name}' is horizontally scaled to {replicas} replicas "
                        "-> scalability: workload runs as multiple interchangeable instances"
                    ),
                    source=_source(file_path, kind, name),
                    confidence=CONFIDENCE_HIGH,
                    evidence={"replicas": replicas},
                )
            )
        else:
            out.append(
                NfrCandidate(
                    category=CAT_AVAILABILITY,
                    statement=(
                        f"{kind} '{name}' runs a single replica -> availability gap: "
                        "single instance is a single point of failure (verify if intended)"
                    ),
                    source=_source(file_path, kind, name),
                    confidence=CONFIDENCE_MEDIUM,
                    evidence={"replicas": replicas},
                )
            )

    out.extend(_k8s_containers(file_path, kind, name, resource))
    return out


def _k8s_other_workload(file_path: str, resource: Any) -> list[NfrCandidate]:
    out: list[NfrCandidate] = []
    kind = resource.get("kind")
    name = resource.get("name") or ""

    if kind == "DaemonSet":
        out.append(
            NfrCandidate(
                category=CAT_TOPOLOGY,
                statement=(
                    f"DaemonSet '{name}' schedules one pod per node -> deployment_topology: "
                    "node-level agent runs across the whole cluster"
                ),
                source=_source(file_path, "DaemonSet", name),
                confidence=CONFIDENCE_HIGH,
                kind=KIND_OPERATIONAL_FACT,
            )
        )
    elif kind in {"Job", "CronJob"}:
        schedule = resource.get("schedule")
        detail = f"on schedule '{schedule}'" if schedule else "as a batch job"
        out.append(
            NfrCandidate(
                category=CAT_TOPOLOGY,
                statement=(
                    f"{kind} '{name}' runs {detail} -> deployment_topology: "
                    "batch/scheduled workload (operational task, not a long-running service)"
                ),
                source=_source(file_path, kind, name),
                confidence=CONFIDENCE_HIGH,
                kind=KIND_OPERATIONAL_FACT,
                evidence={"schedule": schedule} if schedule else {},
            )
        )

    out.extend(_k8s_containers(file_path, kind, name, resource))
    return out


def _k8s_containers(file_path: str, kind: str, name: str, resource: Any) -> list[NfrCandidate]:
    out: list[NfrCandidate] = []
    for container in resource.get("containers", []) or []:
        cname = container.get("name") or ""
        resources = container.get("resources")
        if isinstance(resources, dict) and resources:
            limits = resources.get("limits") or {}
            requests = resources.get("requests") or {}
            out.append(
                NfrCandidate(
                    category=CAT_CAPACITY,
                    statement=(
                        f"Container '{cname}' in {kind} '{name}' declares resource "
                        f"requests/limits (requests={requests or '-'}, limits={limits or '-'}) "
                        "-> performance/capacity: per-instance CPU/memory budget is bounded"
                    ),
                    source=_source(file_path, kind, name, cname),
                    confidence=CONFIDENCE_HIGH,
                    evidence={"requests": requests, "limits": limits},
                )
            )
        probes = container.get("probes")
        if isinstance(probes, dict) and probes:
            out.append(
                NfrCandidate(
                    category=CAT_RELIABILITY,
                    statement=(
                        f"Container '{cname}' in {kind} '{name}' defines health probes "
                        f"({', '.join(sorted(probes))}) -> reliability/health: unhealthy "
                        "instances are detected and replaced/withheld from traffic automatically"
                    ),
                    source=_source(file_path, kind, name, cname),
                    confidence=CONFIDENCE_HIGH,
                    evidence={"probes": sorted(probes)},
                )
            )
    return out


def _k8s_hpa(file_path: str, resource: Any) -> list[NfrCandidate]:
    name = resource.get("name") or ""
    min_r = _as_int(resource.get("min_replicas"))
    max_r = _as_int(resource.get("max_replicas"))
    target = resource.get("scale_target") or {}
    target_desc = ""
    if isinstance(target, dict) and target.get("name"):
        target_desc = f" for {target.get('kind')} '{target.get('name')}'"
    metrics = resource.get("metrics") or []
    metric_names = [m.get("name") for m in metrics if isinstance(m, dict) and m.get("name")]
    metric_desc = f" on metrics={metric_names}" if metric_names else ""

    out = [
        NfrCandidate(
            category=CAT_SCALABILITY,
            statement=(
                f"HorizontalPodAutoscaler '{name}'{target_desc} scales between "
                f"{min_r} and {max_r} replicas{metric_desc} -> scalability: capacity "
                "adapts automatically to load"
            ),
            source=_source(file_path, "HorizontalPodAutoscaler", name),
            confidence=CONFIDENCE_HIGH,
            evidence={"min_replicas": min_r, "max_replicas": max_r, "metrics": metrics},
        )
    ]
    if min_r is not None and min_r >= 2:
        out.append(
            NfrCandidate(
                category=CAT_AVAILABILITY,
                statement=(
                    f"HorizontalPodAutoscaler '{name}' enforces a floor of {min_r} replicas "
                    "-> availability target: tolerate single-instance failure even at minimum load"
                ),
                source=_source(file_path, "HorizontalPodAutoscaler", name),
                confidence=CONFIDENCE_HIGH,
                evidence={"min_replicas": min_r},
            )
        )
    return out


# ---------------------------------------------------------------------------
# Terraform mapping
# ---------------------------------------------------------------------------
def _map_terraform(config: Any) -> list[NfrCandidate]:
    out: list[NfrCandidate] = []
    file_path = getattr(config, "file_path", "")

    for resource in getattr(config, "resources", []) or []:
        if resource.get("kind") != "resource":
            continue
        rtype = str(resource.get("type") or "")
        name = resource.get("name") or ""
        flags = resource.get("nfr_flags") or {}
        provenance = _source(file_path, rtype, name)

        # Security/isolation intent inferred from the resource TYPE.
        lowered_type = rtype.lower()
        if any(token in lowered_type for token in _SECURITY_TYPE_TOKENS):
            out.append(
                NfrCandidate(
                    category=CAT_SECURITY,
                    statement=(
                        f"Terraform resource '{rtype}.{name}' provisions access-control "
                        "infrastructure -> security/isolation: network/identity boundaries "
                        "are explicitly managed"
                    ),
                    source=provenance,
                    confidence=CONFIDENCE_MEDIUM,
                    evidence={"type": rtype},
                )
            )

        if not isinstance(flags, dict):
            continue

        out.extend(_terraform_flag_candidates(provenance, rtype, name, flags))

    return out


def _terraform_flag_candidates(
    provenance: str, rtype: str, name: str, flags: dict[str, Any]
) -> list[NfrCandidate]:
    out: list[NfrCandidate] = []
    label = f"{rtype}.{name}"

    for key, value in flags.items():
        lowered = key.lower()

        if "backup_retention" in lowered:
            retained = _as_int(value)
            if retained is not None and retained > 0:
                out.append(
                    NfrCandidate(
                        category=CAT_DURABILITY,
                        statement=(
                            f"'{label}' retains backups for {retained} (units per provider) "
                            "-> durability/DR: point-in-time recovery window is defined"
                        ),
                        source=provenance,
                        confidence=CONFIDENCE_HIGH,
                        evidence={key: value},
                    )
                )
        elif "multi_az" in lowered:
            if _is_truthy(value):
                out.append(
                    NfrCandidate(
                        category=CAT_AVAILABILITY,
                        statement=(
                            f"'{label}' is multi-AZ -> availability target: tolerate a "
                            "single availability-zone failure"
                        ),
                        source=provenance,
                        confidence=CONFIDENCE_HIGH,
                        evidence={key: value},
                    )
                )
        elif "deletion_protection" in lowered:
            if _is_truthy(value):
                out.append(
                    NfrCandidate(
                        category=CAT_DURABILITY,
                        statement=(
                            f"'{label}' enables deletion protection -> durability/DR: "
                            "resource is guarded against accidental destruction"
                        ),
                        source=provenance,
                        confidence=CONFIDENCE_HIGH,
                        evidence={key: value},
                    )
                )
        elif lowered in {"replicas", "desired_capacity", "min_capacity"} or lowered.endswith("_replicas"):
            count = _as_int(value)
            if count is not None and count >= 2:
                out.append(
                    NfrCandidate(
                        category=CAT_AVAILABILITY,
                        statement=(
                            f"'{label}' provisions {count} instances ({key}) -> availability "
                            "target: tolerate single-instance failure"
                        ),
                        source=provenance,
                        confidence=CONFIDENCE_HIGH,
                        evidence={key: value},
                    )
                )
        elif lowered in {"min_size", "max_size"} or "autoscaling" in lowered:
            out.append(
                NfrCandidate(
                    category=CAT_SCALABILITY,
                    statement=(
                        f"'{label}' declares autoscaling bounds ({key}={value}) -> "
                        "scalability: capacity scales horizontally with load"
                    ),
                    source=provenance,
                    confidence=CONFIDENCE_HIGH,
                    evidence={key: value},
                )
            )
        elif "encrypt" in lowered:
            if _is_truthy(value):
                out.append(
                    NfrCandidate(
                        category=CAT_SECURITY,
                        statement=(
                            f"'{label}' enables encryption ({key}) -> security/isolation: "
                            "data is encrypted at rest"
                        ),
                        source=provenance,
                        confidence=CONFIDENCE_HIGH,
                        evidence={key: value},
                    )
                )

    return out


# ---------------------------------------------------------------------------
# Docker Compose mapping
# ---------------------------------------------------------------------------
def _map_docker_compose(config: Any) -> list[NfrCandidate]:
    out: list[NfrCandidate] = []
    file_path = getattr(config, "file_path", "")
    services = getattr(config, "services", []) or []

    names = [s.get("name") for s in services if s.get("name")]
    if names:
        out.append(
            NfrCandidate(
                category=CAT_TOPOLOGY,
                statement=(
                    f"docker-compose defines {len(names)} co-deployed service(s): "
                    f"{', '.join(str(n) for n in names)} -> deployment_topology: "
                    "multi-service runtime composition"
                ),
                source=_source(file_path),
                confidence=CONFIDENCE_HIGH,
                kind=KIND_OPERATIONAL_FACT,
                evidence={"services": [str(n) for n in names]},
            )
        )

    for service in services:
        sname = service.get("name") or ""
        depends_on = service.get("depends_on") or []
        if depends_on:
            out.append(
                NfrCandidate(
                    category=CAT_TOPOLOGY,
                    statement=(
                        f"Service '{sname}' depends on {depends_on} -> deployment_topology: "
                        "startup/runtime dependency ordering between services"
                    ),
                    source=_source(file_path, sname),
                    confidence=CONFIDENCE_MEDIUM,
                    kind=KIND_OPERATIONAL_FACT,
                    evidence={"depends_on": depends_on},
                )
            )
        volumes = service.get("volumes") or []
        if volumes:
            out.append(
                NfrCandidate(
                    category=CAT_DURABILITY,
                    statement=(
                        f"Service '{sname}' mounts persistent volume(s) -> durability/DR: "
                        "state is stored outside the container lifecycle"
                    ),
                    source=_source(file_path, sname),
                    confidence=CONFIDENCE_MEDIUM,
                    evidence={"volumes": volumes},
                )
            )

    return out


# ---------------------------------------------------------------------------
# GitHub Actions (CI/CD) mapping
# ---------------------------------------------------------------------------
def _map_github_actions(config: Any, environments: set[str]) -> list[NfrCandidate]:
    out: list[NfrCandidate] = []
    file_path = getattr(config, "file_path", "")

    for pipeline in getattr(config, "pipelines", []) or []:
        pname = pipeline.get("name") or ""
        triggers = pipeline.get("triggers") or []
        jobs = pipeline.get("jobs") or []

        step_kinds: set[str] = set()
        for job in jobs:
            env = job.get("environment")
            if env:
                environments.add(str(env))
            for step in job.get("steps", []) or []:
                step_kinds.update(step.get("kinds") or [])

        if "test" in step_kinds:
            out.append(
                NfrCandidate(
                    category=CAT_RELIABILITY,
                    statement=(
                        f"CI workflow '{pname}' runs automated tests on {triggers or 'push'} "
                        "-> reliability/health: changes are gated by an automated test suite"
                    ),
                    source=_source(file_path, pname),
                    confidence=CONFIDENCE_HIGH,
                    evidence={"triggers": triggers, "step_kinds": sorted(step_kinds)},
                )
            )
        if "deploy" in step_kinds:
            out.append(
                NfrCandidate(
                    category=CAT_TOPOLOGY,
                    statement=(
                        f"CI/CD workflow '{pname}' performs automated deployment "
                        "-> deployment_topology: releases are delivered through a CI/CD pipeline"
                    ),
                    source=_source(file_path, pname),
                    confidence=CONFIDENCE_HIGH,
                    kind=KIND_OPERATIONAL_FACT,
                    evidence={"step_kinds": sorted(step_kinds)},
                )
            )

    return out


# ---------------------------------------------------------------------------
# Dockerfile mapping
# ---------------------------------------------------------------------------
def _map_dockerfile(config: Any) -> list[NfrCandidate]:
    out: list[NfrCandidate] = []
    file_path = getattr(config, "file_path", "")

    for image in getattr(config, "images", []) or []:
        stages = image.get("stages") or []
        ports = image.get("ports") or []
        if len(image.get("base_images") or []) >= 2 or stages:
            out.append(
                NfrCandidate(
                    category=CAT_TOPOLOGY,
                    statement=(
                        f"Dockerfile uses a multi-stage build (stages={stages or 'unnamed'}) "
                        "-> deployment_topology: build/runtime image separation for a "
                        "minimal runtime artifact"
                    ),
                    source=_source(file_path),
                    confidence=CONFIDENCE_MEDIUM,
                    kind=KIND_OPERATIONAL_FACT,
                    evidence={"stages": stages, "base_images": image.get("base_images")},
                )
            )
        if ports:
            out.append(
                NfrCandidate(
                    category=CAT_TOPOLOGY,
                    statement=(
                        f"Dockerfile exposes port(s) {ports} -> deployment_topology: "
                        "containerized service network surface"
                    ),
                    source=_source(file_path),
                    confidence=CONFIDENCE_HIGH,
                    kind=KIND_OPERATIONAL_FACT,
                    evidence={"ports": ports},
                )
            )

    return out


# ---------------------------------------------------------------------------
# Ansible (deep-parsed) mapping
# ---------------------------------------------------------------------------
# Module-name → mapping groups. Ansible module names are general tool
# vocabulary (service/apt/cron/ufw/…), not project-specific.
_ANSIBLE_SERVICE_MODULES = {"service", "systemd", "systemd_service", "sysvinit", "supervisorctl", "runit"}
_ANSIBLE_PACKAGE_MODULES = {
    "package",
    "apt",
    "yum",
    "dnf",
    "apk",
    "zypper",
    "pacman",
    "homebrew",
    "pip",
    "npm",
    "gem",
}
_ANSIBLE_FIREWALL_MODULES = {"ufw", "firewalld", "iptables", "nftables"}
_ANSIBLE_ACCOUNT_MODULES = {"user", "group"}


def _map_ansible(config: Any) -> list[NfrCandidate]:
    """Deep-parsed Ansible plays/tasks → deployment topology / reliability / security.

    Upgrades the recognition-only "Ansible present" MEDIUM fact: parsed plays
    and tasks yield specific, HIGH-confidence facts (service supervision, cron
    schedules, firewall rules) with per-task provenance.
    """

    out: list[NfrCandidate] = []
    file_path = getattr(config, "file_path", "")

    for play in getattr(config, "services", []) or []:
        if play.get("kind") != "play":
            continue
        pname = play.get("name") or ""
        hosts = play.get("hosts") or ""
        roles = play.get("roles") or []
        role_desc = f" applying roles {roles}" if roles else ""
        out.append(
            NfrCandidate(
                category=CAT_TOPOLOGY,
                statement=(
                    f"Ansible play '{pname}' provisions host group '{hosts}'"
                    f"{role_desc} -> deployment_topology: host provisioning / "
                    "configuration management is automated"
                ),
                source=_source(file_path, "play", str(pname)),
                confidence=CONFIDENCE_HIGH,
                kind=KIND_OPERATIONAL_FACT,
                evidence={
                    "hosts": hosts,
                    "roles": roles,
                    "task_count": play.get("task_count"),
                },
            )
        )

    for resource in getattr(config, "resources", []) or []:
        kind = resource.get("kind")
        if kind == "role":
            rname = resource.get("name") or ""
            out.append(
                NfrCandidate(
                    category=CAT_TOPOLOGY,
                    statement=(
                        f"Ansible role '{rname}' defines "
                        f"{resource.get('task_count')} reusable provisioning task(s) "
                        "-> deployment_topology: reusable configuration management unit"
                    ),
                    source=_source(file_path, "role", str(rname)),
                    confidence=CONFIDENCE_HIGH,
                    kind=KIND_OPERATIONAL_FACT,
                    evidence={"task_count": resource.get("task_count")},
                )
            )
            continue
        if kind not in {"task", "handler"}:
            continue
        out.extend(_ansible_task_candidates(file_path, resource))

    return out


def _ansible_task_candidates(file_path: str, task: dict[str, Any]) -> list[NfrCandidate]:
    module = str(task.get("module") or "")
    tname = str(task.get("name") or module)
    attrs = task.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    provenance = _source(file_path, str(task.get("kind") or "task"), tname)
    subject = str(attrs.get("name") or tname)

    if module in _ANSIBLE_SERVICE_MODULES:
        detail_parts = [
            f"{key}={attrs[key]}" for key in ("state", "enabled") if key in attrs
        ]
        detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
        return [
            NfrCandidate(
                category=CAT_RELIABILITY,
                statement=(
                    f"Ansible manages service '{subject}'{detail} as a system service "
                    "-> reliability/health: the service is supervised by the init "
                    "system and kept in its declared state"
                ),
                source=provenance,
                confidence=CONFIDENCE_HIGH,
                kind=KIND_OPERATIONAL_FACT,
                evidence={"module": module, **attrs},
            )
        ]
    if module in _ANSIBLE_PACKAGE_MODULES:
        return [
            NfrCandidate(
                category=CAT_TOPOLOGY,
                statement=(
                    f"Ansible installs package '{subject}' via {module} -> "
                    "deployment_topology: host-level runtime dependency is "
                    "explicitly provisioned"
                ),
                source=provenance,
                confidence=CONFIDENCE_MEDIUM,
                kind=KIND_OPERATIONAL_FACT,
                evidence={"module": module, **attrs},
            )
        ]
    if module == "cron":
        schedule_parts = [
            f"{key}={attrs[key]}"
            for key in ("minute", "hour", "day", "weekday", "month", "special_time")
            if key in attrs
        ]
        schedule = f" ({', '.join(schedule_parts)})" if schedule_parts else ""
        return [
            NfrCandidate(
                category=CAT_TOPOLOGY,
                statement=(
                    f"Ansible schedules cron job '{subject}'{schedule} -> "
                    "deployment_topology: recurring batch/scheduled workload "
                    "(operational task, not a long-running service)"
                ),
                source=provenance,
                confidence=CONFIDENCE_HIGH,
                kind=KIND_OPERATIONAL_FACT,
                evidence={"module": module, **attrs},
            )
        ]
    if module in _ANSIBLE_FIREWALL_MODULES:
        return [
            NfrCandidate(
                category=CAT_SECURITY,
                statement=(
                    f"Ansible manages firewall rule '{tname}' via {module} -> "
                    "security/isolation: host network access is explicitly restricted"
                ),
                source=provenance,
                confidence=CONFIDENCE_HIGH,
                kind=KIND_NFR,
                evidence={"module": module, **attrs},
            )
        ]
    if module in _ANSIBLE_ACCOUNT_MODULES:
        return [
            NfrCandidate(
                category=CAT_SECURITY,
                statement=(
                    f"Ansible manages {module} '{subject}' -> security/isolation: "
                    "OS accounts/groups are explicitly managed (least-privilege evidence)"
                ),
                source=provenance,
                confidence=CONFIDENCE_MEDIUM,
                kind=KIND_OPERATIONAL_FACT,
                evidence={"module": module, **attrs},
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Prometheus / Alertmanager (deep-parsed) mapping
# ---------------------------------------------------------------------------
def _map_prometheus(config: Any) -> list[NfrCandidate]:
    """Deep-parsed Prometheus facts → observability/SLO candidates.

    Each alerting rule is a machine-readable *candidate acceptance criterion*:
    the alert's threshold expression, sustain duration and severity prescribe a
    condition the running system must not violate. These upgrade the previous
    recognition-only MEDIUM fact to HIGH-confidence, per-rule NFR candidates.
    """

    out: list[NfrCandidate] = []
    file_path = getattr(config, "file_path", "")
    receiver_names: list[str] = []
    route_evidence: dict[str, Any] | None = None

    for resource in getattr(config, "resources", []) or []:
        kind = resource.get("kind")
        name = str(resource.get("name") or "")
        attrs = resource.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}

        if kind == "AlertRule":
            expr = attrs.get("expr") or ""
            duration = attrs.get("for")
            severity = attrs.get("severity")
            for_desc = f" sustained for {duration}" if duration else ""
            severity_desc = f" at severity {severity}" if severity else ""
            out.append(
                NfrCandidate(
                    category=CAT_OBSERVABILITY,
                    statement=(
                        f"Alert '{name}' fires when `{expr}`{for_desc}{severity_desc} "
                        "-> candidate SLO/acceptance criterion: in normal operation the "
                        f"monitored condition `{expr}` must not hold"
                        f"{' beyond ' + str(duration) if duration else ''}"
                    ),
                    source=_source(file_path, "AlertRule", name),
                    confidence=CONFIDENCE_HIGH,
                    kind=KIND_NFR,
                    evidence={
                        key: attrs[key]
                        for key in ("expr", "for", "severity", "group", "summary", "description")
                        if key in attrs
                    },
                )
            )
        elif kind == "RecordingRule":
            out.append(
                NfrCandidate(
                    category=CAT_OBSERVABILITY,
                    statement=(
                        f"Recording rule '{name}' precomputes `{attrs.get('expr') or ''}` "
                        "-> observability/SLO: a derived metric is part of the "
                        "monitoring model (likely an SLI input)"
                    ),
                    source=_source(file_path, "RecordingRule", name),
                    confidence=CONFIDENCE_HIGH,
                    kind=KIND_OPERATIONAL_FACT,
                    evidence={"expr": attrs.get("expr"), "group": attrs.get("group")},
                )
            )
        elif kind == "ScrapeJob":
            out.append(
                NfrCandidate(
                    category=CAT_OBSERVABILITY,
                    statement=(
                        f"Prometheus scrapes job '{name}' "
                        f"({attrs.get('targets_count', 0)} static target(s)) -> "
                        "observability/SLO: the component is under active metrics monitoring"
                    ),
                    source=_source(file_path, "ScrapeJob", name),
                    confidence=CONFIDENCE_MEDIUM,
                    kind=KIND_OPERATIONAL_FACT,
                    evidence={"targets_count": attrs.get("targets_count")},
                )
            )
        elif kind == "AlertmanagerReceiver":
            receiver_names.append(name)
        elif kind == "AlertmanagerRoute":
            route_evidence = {
                "default_receiver": name,
                "child_routes": attrs.get("child_routes"),
            }

    if receiver_names or route_evidence:
        evidence: dict[str, Any] = {}
        if receiver_names:
            evidence["receivers"] = receiver_names
        if route_evidence:
            evidence.update(route_evidence)
        receiver_desc = (
            f"{len(receiver_names)} receiver(s): {', '.join(receiver_names)}"
            if receiver_names
            else "a route tree"
        )
        out.append(
            NfrCandidate(
                category=CAT_OBSERVABILITY,
                statement=(
                    f"Alertmanager routes alerts to {receiver_desc} -> "
                    "observability/SLO: alert routing and on-call escalation are configured"
                ),
                source=_source(file_path, "Alertmanager"),
                confidence=CONFIDENCE_MEDIUM,
                kind=KIND_OPERATIONAL_FACT,
                evidence=evidence,
            )
        )

    return out


# ---------------------------------------------------------------------------
# Ops-evidence (recognition-only) mapping
# ---------------------------------------------------------------------------
_OPS_EVIDENCE_MAP: dict[str, tuple[str, str, str]] = {
    # recognized_kind -> (category, kind, statement-suffix)
    "prometheus_rules": (
        CAT_OBSERVABILITY,
        KIND_NFR,
        "Prometheus alerting/recording rules present -> observability/SLO: "
        "alert thresholds are a candidate source of acceptance criteria / SLOs",
    ),
    "prometheus_config": (
        CAT_OBSERVABILITY,
        KIND_OPERATIONAL_FACT,
        "Prometheus configuration present -> observability/SLO: metrics scraping "
        "is configured (monitoring topology evidence)",
    ),
    "alertmanager_config": (
        CAT_OBSERVABILITY,
        KIND_OPERATIONAL_FACT,
        "Alertmanager configuration present -> observability/SLO: alert routing "
        "and on-call escalation are configured",
    ),
    "helm_chart": (
        CAT_TOPOLOGY,
        KIND_OPERATIONAL_FACT,
        "Helm chart present -> deployment_topology: application is packaged for "
        "templated, parameterized deployment",
    ),
    "ansible_playbook": (
        CAT_TOPOLOGY,
        KIND_OPERATIONAL_FACT,
        "Ansible playbook present -> deployment_topology: configuration management "
        "/ provisioning is automated",
    ),
    "ansible_role": (
        CAT_TOPOLOGY,
        KIND_OPERATIONAL_FACT,
        "Ansible role present -> deployment_topology: reusable provisioning "
        "configuration is defined",
    ),
}


def _map_ops_evidence(config: Any) -> list[NfrCandidate]:
    file_path = getattr(config, "file_path", "")
    recognized_kind = getattr(config, "recognized_kind", "")
    mapped = _OPS_EVIDENCE_MAP.get(recognized_kind)
    if not mapped:
        return []
    category, kind, statement = mapped
    return [
        NfrCandidate(
            category=category,
            statement=statement,
            source=_source(file_path),
            confidence=CONFIDENCE_MEDIUM,
            kind=kind,
            evidence={"recognized_kind": recognized_kind},
        )
    ]


# ---------------------------------------------------------------------------
# Cross-resource deployment topology
# ---------------------------------------------------------------------------
def _map_topology(namespaces: set[str], gha_environments: set[str]) -> list[NfrCandidate]:
    out: list[NfrCandidate] = []

    if len(namespaces) >= 2:
        ordered = sorted(namespaces)
        out.append(
            NfrCandidate(
                category=CAT_TOPOLOGY,
                statement=(
                    f"Workloads span {len(ordered)} Kubernetes namespaces "
                    f"({', '.join(ordered)}) -> deployment_topology: multi-environment / "
                    "multi-tenant isolation matrix"
                ),
                source="kubernetes::namespaces",
                confidence=CONFIDENCE_MEDIUM,
                kind=KIND_OPERATIONAL_FACT,
                evidence={"namespaces": ordered},
            )
        )

    if len(gha_environments) >= 2:
        ordered_env = sorted(gha_environments)
        out.append(
            NfrCandidate(
                category=CAT_TOPOLOGY,
                statement=(
                    f"CI/CD targets {len(ordered_env)} deployment environments "
                    f"({', '.join(ordered_env)}) -> deployment_topology: promotion across an "
                    "environment matrix"
                ),
                source="github-actions::environments",
                confidence=CONFIDENCE_MEDIUM,
                kind=KIND_OPERATIONAL_FACT,
                evidence={"environments": ordered_env},
            )
        )

    return out
