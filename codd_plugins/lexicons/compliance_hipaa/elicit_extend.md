---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: compliance_hipaa
observation_dimensions: 10
---

# HIPAA Compliance Observation Dimensions

Apply the base elicitation prompt, then inspect requirements and design notes
through these HIPAA Security Rule and breach-notification dimensions:

1. `Administrative safeguards`
2. `Physical safeguards`
3. `Technical safeguards`
4. `Risk analysis`
5. `Access control`
6. `Audit controls`
7. `Integrity`
8. `Transmission security`
9. `Breach notification`
10. `Business associate contracts or other arrangements`

For each dimension, classify coverage as:

- `covered`: requirements explicitly state the safeguard, control, workflow,
  non-applicability, or regulated-party responsibility for ePHI or PHI handling.
- `implicit`: the material makes the dimension irrelevant, such as no ePHI, no
  PHI, no business associate, or a documented upstream covered entity owns the
  control completely.
- `gap`: ePHI, PHI, a covered entity, business associate, or likely regulated
  workflow exists and the dimension can affect confidentiality, integrity,
  availability, incident response, notification, or auditability.

Emit findings only for dimensions classified as `gap`. Include the dimension in
`details.dimension`, the ePHI/PHI workflow or omission in `details.evidence`,
and a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: A telehealth design states risk analysis, role-based ePHI access,
  audit log review, transmission encryption, facility/device controls, business
  associate agreements, and breach escalation; classify `Risk analysis`,
  `Access control`, `Audit controls`, `Transmission security`, `Physical
  safeguards`, `Business associate contracts or other arrangements`, and
  `Breach notification` as `covered`.
- `implicit`: A wellness landing page stores no identifiers, has no account
  data, and documents that it does not create, receive, maintain, or transmit
  ePHI; classify all HIPAA dimensions as `implicit`.
- `gap`: A patient messaging service stores ePHI and uses a cloud vendor but
  does not state risk analysis, audit controls, transmission security, or a
  business associate contract; classify `Risk analysis`, `Audit controls`,
  `Transmission security`, and `Business associate contracts or other
  arrangements` as `gap`.

Use recommended kinds as guidance. Do not invent dimensions outside the ten
listed above.
