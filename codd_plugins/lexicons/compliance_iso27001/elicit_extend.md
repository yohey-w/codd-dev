---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: compliance_iso27001
observation_dimensions: 14
---

# ISO/IEC 27001 Compliance Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the 14 ISO/IEC 27001 axes declared in `lexicon.yaml`. Use ISMS clause
terms for management-system coverage and ISO information-security control
vocabulary for control coverage.

1. `context_organization`
2. `leadership`
3. `planning`
4. `support`
5. `operation`
6. `performance_evaluation`
7. `improvement`
8. `risk_treatment_plan`
9. `SOA`
10. `access_control`
11. `cryptography`
12. `physical_security`
13. `supplier_relationships`
14. `incident_management`

For every axis, classify coverage as:

- `covered`: the material explicitly states the ISMS clause expectation or
  control area, including owner, evidence, scope, and operating behavior.
- `implicit`: the material references a shared ISO/IEC 27001 baseline, policy,
  Statement of Applicability, or control set that is present in the same source
  set and clearly covers the axis.
- `gap`: the material omits the management-system or control detail needed to
  judge security governance, risk treatment, control applicability, or control
  operation.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing compliance behavior requires human
confirmation. Severity follows `severity_rules.yaml`.

## Coverage-check examples

### covered

Requirement: "The ISMS scope includes the payment platform, leadership owns the
information security policy, the risk treatment plan selects controls, and the
Statement of Applicability justifies access-control and incident-management
controls."

Classification: `covered` for `context_organization`, `leadership`,
`risk_treatment_plan`, `SOA`, `access_control`, and `incident_management`.

### implicit

Requirement: "This product inherits the attached ISO/IEC 27001 control baseline
`isms-standard-v2`, including supplier agreements, cryptographic controls,
physical controls, internal audit, and continual improvement."

Classification: `implicit` for `supplier_relationships`, `cryptography`,
`physical_security`, `performance_evaluation`, and `improvement` when the
referenced baseline is available in the same source set.

### gap

Requirement: "The system must be ISO 27001 compliant."

Classification: `gap` for all axes because the material does not define scope,
responsibility, risk treatment, Statement of Applicability, or control coverage.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.

