# DORA Metrics and SRE Principles Coverage Matrix

Source: DORA Accelerate research and the Google SRE Book.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `deployment_frequency` | Production deployment cadence | The material defines how often changes are released or deployed. | Release cadence is absent or only described informally. |
| `lead_time_for_changes` | Change delivery latency | Commit-to-production or change-to-production elapsed time is measured. | Delivery latency cannot be measured from the stated process. |
| `change_failure_rate` | Deployment-caused failures | Failed deployments, rollback, incident, or hotfix percentage is tracked. | Release stability is not measured after deployment. |
| `mean_time_to_restore` | Incident recovery time | Restore time after user-visible failure is measured and owned. | Recovery speed is not defined or is only ad hoc. |
| `slo_sli_definition` | User-visible reliability contract | SLIs and SLOs state what is measured and which target applies. | Reliability is asserted without measurable service-level targets. |
| `error_budget_policy` | Reliability risk governance | Error budget tracking, burn-rate alerting, or release policy is declared. | Reliability targets do not affect alerting or release decisions. |
| `toil_reduction` | Repetitive operations work | Manual repetitive work is identified and has an automation or reduction path. | Manual operations can grow without review or engineering follow-up. |
| `incident_management` | Response and learning process | On-call, runbook, escalation, restoration, and postmortem behavior is explicit. | Incidents rely on unnamed or informal response behavior. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.
