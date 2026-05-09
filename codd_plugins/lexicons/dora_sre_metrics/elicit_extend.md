---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: dora_sre_metrics
observation_dimensions: 8
---

# DORA Metrics and SRE Principles Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the 8 operational axes declared in `lexicon.yaml`. Use DORA vocabulary
for delivery performance and SRE vocabulary for user-visible reliability,
budgets, toil, and incidents.

1. `deployment_frequency`
2. `lead_time_for_changes`
3. `change_failure_rate`
4. `mean_time_to_restore`
5. `slo_sli_definition`
6. `error_budget_policy`
7. `toil_reduction`
8. `incident_management`

For every axis, classify coverage as:

- `covered`: the material explicitly states the operational metric, reliability
  target, response process, or improvement mechanism and names the owner or
  observable signal that makes it verifiable.
- `implicit`: the material refers to a shared operations, SRE, or delivery
  baseline that is available in the same source set and clearly covers the axis.
- `gap`: the material omits an operational contract needed to judge delivery
  throughput, release stability, restoration, SLOs, error budgets, toil, or
  incident response.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing operational behavior requires human
confirmation. Severity follows `severity_rules.yaml`.

## Coverage-check examples

### covered

Requirement: "The service tracks deployment frequency, lead time for changes,
change failure rate, and MTTR on the release dashboard. Production SLOs are
defined from latency and availability SLIs, and burn-rate alerts page the
on-call engineer when the error budget is at risk."

Classification: `covered` for `deployment_frequency`,
`lead_time_for_changes`, `change_failure_rate`, `mean_time_to_restore`,
`slo_sli_definition`, and `error_budget_policy`.

### implicit

Requirement: "All services follow the attached SRE operating baseline
`ops-standard-v2`, which defines runbooks, postmortem ownership, and the toil
review process."

Classification: `implicit` for `incident_management` and `toil_reduction` when
the referenced baseline is available in the same source set.

### gap

Requirement: "Deployments are automated and the team responds to alerts during
business hours."

Classification: `gap` for `slo_sli_definition`, `error_budget_policy`,
`mean_time_to_restore`, and `incident_management` because the material does not
define reliability targets, budget policy, restoration measurement, or response
process.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
