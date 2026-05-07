---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: ops_cicd_pipeline
observation_dimensions: 7
---

# CI/CD Pipeline GitOps Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the 7 GitOps pipeline axes declared in `lexicon.yaml`. Use OpenGitOps
terms for desired state, immutability, automatic pull, and continuous
reconciliation, and use generic pipeline vocabulary for drift, rollback, and
observability.

1. `declarative_config`
2. `version_control`
3. `automated_apply`
4. `continuous_reconciliation`
5. `drift_detection`
6. `rollback`
7. `observability`

For every axis, classify coverage as:

- `covered`: the material explicitly states the pipeline expectation and the
  desired-state, agent, reconciliation, recovery, or evidence mechanism that
  owns it.
- `implicit`: the material references a shared GitOps or delivery baseline that
  is present in the same source set and clearly covers the axis.
- `gap`: the material omits a pipeline contract needed to judge state
  declaration, change history, application, convergence, drift, rollback, or
  observability.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing pipeline behavior requires human
confirmation. Severity follows `severity_rules.yaml`.

## Coverage-check examples

### covered

Requirement: "Desired state is stored as declarative configuration, reviewed in
version control, pulled automatically by a software agent, continuously
reconciled, and reported with sync status."

Classification: `covered` for `declarative_config`, `version_control`,
`automated_apply`, `continuous_reconciliation`, and `observability`.

### implicit

Requirement: "All production services follow the attached GitOps delivery
baseline `delivery-standard-v3`, which defines drift detection, rollback from
previous revisions, and reconciliation status."

Classification: `implicit` for `drift_detection`, `rollback`, and
`observability` when the referenced baseline is available in the same source set.

### gap

Requirement: "The release engineer deploys the service after merge and checks it
manually."

Classification: `gap` for `automated_apply`, `continuous_reconciliation`,
`drift_detection`, and `observability` because the material does not define an
agent pull path, convergence loop, state-drift signal, or inspectable pipeline
evidence.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.

