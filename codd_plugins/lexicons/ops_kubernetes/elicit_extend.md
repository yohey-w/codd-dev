---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: ops_kubernetes
observation_dimensions: 12
---

# Kubernetes Operations Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the 12 Kubernetes axes declared in `lexicon.yaml`. Use Kubernetes API
Reference v1.30 and Kubernetes Concepts terms for workloads, services,
configuration, storage, RBAC, scheduling, resources, probes, autoscaling,
observability, upgrades, and namespaces.

1. `Workloads`
2. `Service`
3. `ConfigMap`
4. `PersistentVolume`
5. `RBAC`
6. `Scheduling`
7. `ResourceQuota`
8. `Probe`
9. `HorizontalPodAutoscaler`
10. `Events`
11. `Upgrade A Cluster`
12. `Namespace`

For every axis, classify coverage as:

- `covered`: the material explicitly states the Kubernetes resource, field, or
  task that owns the operational behavior.
- `implicit`: the material references a Kubernetes baseline that is present in
  the same source set and clearly covers the axis.
- `gap`: the material omits a Kubernetes contract needed to judge runtime,
  access, networking, capacity, health, scaling, observability, upgrade, or
  tenant behavior.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing Kubernetes behavior needs human
confirmation. Severity follows `severity_rules.yaml`; RBAC gaps are usually
`critical`, workload, networking, configuration, storage, resource, probe,
autoscaling, and namespace gaps are usually `high`, and scheduling,
observability, and upgrade gaps are usually `medium` unless the material makes
them release-blocking.

## Coverage-check examples

### covered

Requirement: "The API runs as a `Deployment` with a `Service`, `ConfigMap`,
`Secret`, `readinessProbe`, `livenessProbe`, CPU `requests` and `limits`, a
`HorizontalPodAutoscaler`, and a namespaced `RoleBinding` for its
`ServiceAccount`."

Classification: `covered` for `Workloads`, `Service`, `ConfigMap`, `Probe`,
`ResourceQuota`, `HorizontalPodAutoscaler`, `RBAC`, and `Namespace` because the
Kubernetes resources and fields are explicit.

### implicit

Requirement: "All workloads inherit the platform Kubernetes baseline
`cluster-standard-v3`, which defines `NetworkPolicy`, `ResourceQuota`,
`LimitRange`, default probes, audit collection, and upgrade disruption budgets."

Classification: `implicit` for `Service`, `ResourceQuota`, `Probe`, `Events`,
`Upgrade A Cluster`, and `Namespace` when the referenced baseline is available
in the same source set and covers those details.

### gap

Requirement: "The worker must persist state, scale under load, and be isolated
from other tenants."

Classification: `gap` for `PersistentVolume`, `HorizontalPodAutoscaler`, and
`Namespace` when the material does not specify PersistentVolumeClaim,
HorizontalPodAutoscaler metrics or bounds, or namespace isolation controls.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
