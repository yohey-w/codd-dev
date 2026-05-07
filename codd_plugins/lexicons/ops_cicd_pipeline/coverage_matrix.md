# CI/CD Pipeline GitOps Coverage Matrix

Source: OpenGitOps Principles v1.0.0, with CNCF GitOps project context.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `declarative_config` | Desired state | The desired state is declarative and reproducible from configuration. | Deployment behavior is only manual steps or prose. |
| `version_control` | Immutable history | Desired state has immutable, versioned, reviewable history. | Release state can change without version history or review. |
| `automated_apply` | Agent pull and apply | Software agents pull desired declarations from the source and apply them. | Humans apply changes outside the pipeline contract. |
| `continuous_reconciliation` | Desired-versus-actual convergence | Actual state is observed continuously and reconciled to desired state. | The pipeline deploys once and stops checking actual state. |
| `drift_detection` | State divergence | Drift, out-of-sync, or health status is explicit. | Runtime state can differ from declared state without detection. |
| `rollback` | Recovery path | Previous revisions or release history define a rollback path. | Failed releases have no declared recovery point. |
| `observability` | Pipeline evidence | Sync, reconciliation, health, and audit signals are inspectable. | Operators cannot diagnose deployment state or failure cause. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.

