---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: data_nosql_jsonschema
observation_dimensions: 10
---

# JSON Schema 2020-12 Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through the 10
JSON Schema 2020-12 axes declared in `lexicon.yaml`. Use JSON Schema Core and
Validation keywords literally. Do not infer storage engine, database product, or
document store behavior unless it is expressed as JSON Schema vocabulary,
keyword, dialect, or validation semantics.

1. `JSON Schema Documents`
2. `Schema Vocabularies`
3. `Meta-Schemas`
4. `Identifiers`
5. `References`
6. `Applicators`
7. `Assertions`
8. `Annotations`
9. `Validation Keywords`
10. `Format`

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the JSON Schema keyword,
  vocabulary, dialect, meta-schema, reference, applicator, assertion,
  annotation, or validation behavior that owns the axis.
- `implicit`: the requirement refers to an attached JSON Schema document or
  standard profile that is present in the same material and clearly covers the
  axis.
- `gap`: the material omits the schema detail required to judge the axis.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing JSON Schema behavior requires human
confirmation. Severity follows `severity_rules.yaml`; meta-schema, identifier,
reference, applicator, assertion, and structural validation gaps are usually
`high`, while annotation and format gaps are usually `medium` unless the
material makes them release-blocking.

## Coverage-check examples

### covered

Requirement: "The customer document uses JSON Schema 2020-12 with `$schema`,
`$id`, `$defs/address`, `type: object`, `required: [id, email]`,
`additionalProperties: false`, and `format: email`."

Classification: `covered` for `Meta-Schemas`, `Identifiers`, `References`,
`Assertions`, `Validation Keywords`, and `Format`.

### implicit

Requirement: "All event payloads conform to the attached shared JSON Schema
dialect profile, including vocabularies, meta-schema, references, object
properties, and tuple arrays."

Classification: `implicit` for `Schema Vocabularies`, `Meta-Schemas`,
`References`, `Applicators`, and `Validation Keywords` when the attached profile
is present and covers those details.

### gap

Requirement: "A profile object may contain contact details and arbitrary tags."

Classification: `gap` for `Validation Keywords` if required properties,
`properties`, `items`, or `additionalProperties` behavior is absent, and `gap`
for `Format` if contact strings require semantic validation but no `format`
expectation is stated.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
