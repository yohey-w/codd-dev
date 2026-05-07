---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: data_governance_appi_gdpr
observation_dimensions: 12
---

# GDPR and APPI Data Governance Observation Dimensions

Apply the base elicitation prompt, then inspect requirements and design notes
through these GDPR/APPI personal-data governance dimensions:

1. `Lawfulness of processing`
2. `Consent`
3. `Purpose limitation`
4. `Storage limitation`
5. `Data subject rights`
6. `Responsibility of the controller`
7. `Processor`
8. `Records of processing activities`
9. `Data protection impact assessment`
10. `Notification of a personal data breach`
11. `Transfers of personal data to third countries or international organisations`
12. `Data protection officer`

For each dimension, classify coverage as:

- `covered`: requirements explicitly state the obligation, non-applicability, or
  workflow expected for the personal-data processing context.
- `implicit`: the material makes the dimension irrelevant, such as no personal
  data, no retained data, no processor, or no cross-border transfer.
- `gap`: personal data or likely personal-data processing exists and the
  dimension can affect rights, obligations, auditability, or incident handling.

Emit findings only for dimensions classified as `gap`. Include the dimension in
`details.dimension`, the personal-data flow or omission in `details.evidence`,
and a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: A patient portal requirement states the lawful basis, utilization
  purpose, retention period, disclosure/correction workflow, processor contract,
  and breach escalation; classify `Lawfulness of processing`, `Purpose
  limitation`, `Storage limitation`, `Data subject rights`, `Processor`, and
  `Notification of a personal data breach` as `covered`.
- `implicit`: A static marketing page stores no identifiers, has no forms, and
  documents that analytics is disabled; classify all personal-data dimensions as
  `implicit`.
- `gap`: A SaaS design stores customer profiles in an offshore cloud provider
  but does not state lawful basis, foreign-country safeguards, or requester
  workflows; classify `Lawfulness of processing`, `Transfers of personal data to
  third countries or international organisations`, and `Data subject rights` as
  `gap`.

Use recommended kinds as guidance. Do not invent dimensions outside the twelve
listed above.
