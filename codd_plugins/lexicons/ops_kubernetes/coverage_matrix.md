# Kubernetes Operations Coverage Matrix

Source: Kubernetes API Reference v1.30 plus Kubernetes Concepts and Tasks.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `Workloads` | Pod and controller execution | Pods and controller kinds such as Deployment, StatefulSet, DaemonSet, Job, or CronJob are explicit. | A service is expected to run but no workload resource or controller ownership is named. |
| `Service` | Workload reachability | Service, Endpoints or EndpointSlice, Ingress, and NetworkPolicy expectations are explicit where relevant. | Workloads are said to be reachable without endpoint, routing, or traffic policy details. |
| `ConfigMap` | Configuration and sensitive data injection | ConfigMap, Secret, env, volumeMounts, or projected volume behavior is explicit. | Runtime configuration or sensitive data is needed but no Kubernetes injection mechanism is declared. |
| `PersistentVolume` | Persistent storage | PersistentVolume, PersistentVolumeClaim, StorageClass, Volume, or volumeClaimTemplates are explicit. | Stateful behavior exists without storage claim, class, or mount expectations. |
| `RBAC` | Kubernetes API authorization | ServiceAccount, Role, ClusterRole, RoleBinding, and ClusterRoleBinding ownership are explicit. | A component needs API access but permissions or subject binding are absent. |
| `Scheduling` | Pod placement | nodeSelector, affinity, taints, tolerations, topologySpreadConstraints, or schedulerName are explicit. | Placement or topology requirements are only described informally. |
| `ResourceQuota` | Capacity governance | requests, limits, ResourceQuota, LimitRange, or QoS expectations are explicit. | Capacity or tenant fairness is expected without Kubernetes resource controls. |
| `Probe` | Container health | livenessProbe, readinessProbe, startupProbe, or Probe behavior is explicit. | Availability is expected but restart/readiness checks are absent. |
| `HorizontalPodAutoscaler` | Workload scaling | HorizontalPodAutoscaler, scaleTargetRef, metrics, minReplicas, maxReplicas, and behavior are explicit. | Autoscaling is expected without targets, metrics, or replica bounds. |
| `Events` | Operational visibility | Event, metrics.k8s.io, custom.metrics.k8s.io, external.metrics.k8s.io, or audit.k8s.io coverage is explicit. | Operators need diagnosis or auditability without Kubernetes observability sources. |
| `Upgrade A Cluster` | Version and disruption management | Upgrade task, kubectl drain, cordon, PodDisruptionBudget, and API deprecation checks are explicit. | Cluster upgrades are planned without drain, disruption, or compatibility criteria. |
| `Namespace` | Multi-tenant scoping | Namespace, quota, limits, access binding, and network isolation are explicit. | Multiple teams or tenants share a cluster without Kubernetes isolation controls. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
