---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: ops_iac_terraform
observation_dimensions: 10
---

# Terraform IaC Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the 10 Terraform axes declared in `lexicon.yaml`. Use Terraform
Language v1.7.x terms for blocks, providers, resources, data sources, variables,
outputs, state, modules, workspaces, backends, and Sentinel policy enforcement.

1. `terraform`
2. `provider`
3. `resource`
4. `data`
5. `variable`
6. `state`
7. `module`
8. `workspace`
9. `backend`
10. `Sentinel`

For every axis, classify coverage as:

- `covered`: the material explicitly states the Terraform block, argument,
  state concept, workspace behavior, backend behavior, or Sentinel policy
  construct that owns the infrastructure-as-code behavior.
- `implicit`: the material references a Terraform baseline that is present in
  the same source set and clearly covers the axis.
- `gap`: the material omits a Terraform contract needed to judge configuration,
  provider, resource, data, module, state, workspace, backend, or policy
  behavior.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing Terraform behavior needs human
confirmation. Severity follows `severity_rules.yaml`; `resource` gaps are
usually `critical`, settings, provider, variable, state, module, and backend
gaps are usually `high`, and data, workspace, and Sentinel gaps are usually
`medium` unless the material makes them release-blocking.

## Coverage-check examples

### covered

Requirement: "The module declares `required_version`, `required_providers`, a
`provider` with version constraints, `resource` blocks with `for_each`,
validated `variable` inputs, `output` values marked `sensitive` where needed,
a `backend` initialized by `terraform init`, and a Sentinel `policy set` with
`enforcement level` documented."

Classification: `covered` for `terraform`, `provider`, `resource`, `variable`,
`backend`, and `Sentinel` because the Terraform language and policy constructs
are explicit.

### implicit

Requirement: "All modules inherit the platform Terraform baseline
`iac-standard-v4`, which defines provider aliases, backend partial
configuration, workspace state policy, import and moved block conventions, and
Sentinel policy sets."

Classification: `implicit` for `provider`, `backend`, `workspace`, `state`, and
`Sentinel` when the referenced baseline is available in the same source set and
covers those details.

### gap

Requirement: "Provision the shared queue module in every environment and reject
non-compliant plans."

Classification: `gap` for `module`, `workspace`, and `Sentinel` when the
material does not specify module `source` or `version`, workspace/state
selection, or policy set and enforcement level details.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
