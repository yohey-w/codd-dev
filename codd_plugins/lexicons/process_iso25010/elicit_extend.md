---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: process_iso25010
observation_dimensions: 8
---

# ISO/IEC 25010 Product Quality Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the 8 ISO/IEC 25010:2011 product quality axes declared in
`lexicon.yaml`.

1. `functional_suitability`
2. `performance_efficiency`
3. `compatibility`
4. `usability`
5. `reliability`
6. `security`
7. `maintainability`
8. `portability`

For every axis, classify coverage as:

- `covered`: the material explicitly states the quality characteristic and the
  measurable sub-characteristic, context, or acceptance evidence that owns it.
- `implicit`: the material references a shared quality model, nonfunctional
  baseline, or acceptance policy that is present in the same source set and
  clearly covers the axis.
- `gap`: the material omits a quality contract needed to judge function,
  performance, compatibility, usability, reliability, security, maintainability,
  or portability.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing quality behavior requires human
confirmation. Severity follows `severity_rules.yaml`.

## Coverage-check examples

### covered

Requirement: "The export function must produce correct results for all selected
records, respond within two seconds at 5,000 records, recover after worker
restart, and log accountable user actions."

Classification: `covered` for `functional_suitability`,
`performance_efficiency`, `reliability`, and `security`.

### implicit

Requirement: "All admin pages follow the attached product-quality baseline
`quality-standard-v4`, including operability, accessibility, compatibility,
testability, and installability requirements."

Classification: `implicit` for `usability`, `compatibility`,
`maintainability`, and `portability` when the referenced baseline is available
in the same source set.

### gap

Requirement: "The new dashboard must be high quality and production ready."

Classification: `gap` for all axes because the material does not state
functions, performance, compatibility, usability, reliability, security,
maintainability, or portability criteria.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.

