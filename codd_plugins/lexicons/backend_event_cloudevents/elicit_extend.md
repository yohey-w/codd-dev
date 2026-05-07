---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: backend_event_cloudevents
observation_dimensions: 7
---

# CloudEvents 1.0.2 Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through the 7
CloudEvents 1.0.2 axes declared in `lexicon.yaml`. Use CloudEvents source
literals. Do not infer a broker, queue, event router, cloud provider, or storage
product unless it is expressed by the requirement.

1. `Context Attributes`
2. `REQUIRED Attributes`
3. `OPTIONAL Attributes`
4. `Type System`
5. `Extension Context Attributes`
6. `Event Data`
7. `Protocol Binding`

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the CloudEvents attribute,
  payload, type, extension, event format, or binding behavior that owns the
  axis.
- `implicit`: the requirement references a concrete CloudEvents profile or SDK
  contract that already covers the axis and no local override is required.
- `gap`: the material describes event exchange but omits the CloudEvents detail
  needed to judge conformance or interoperability.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing event behavior requires human
confirmation. Severity follows `severity_rules.yaml`.

## Coverage-check examples

### covered

Requirement: "The order event is a CloudEvent with `specversion: 1.0`, `id`,
`source`, `type`, `time`, `datacontenttype: application/json`, a `dataschema`
URI, and binary-mode delivery over the selected protocol binding."

Classification: `covered` for `REQUIRED Attributes`, `OPTIONAL Attributes`,
`Event Data`, and `Protocol Binding`.

### implicit

Requirement: "The service emits events through the shared CloudEvents 1.0.2
profile attached to this API contract."

Classification: `implicit` for `Context Attributes`, `REQUIRED Attributes`, and
`Type System` when the attached profile defines those details.

### gap

Requirement: "Publish a user-updated event for downstream consumers."

Classification: `gap` for `REQUIRED Attributes` if `id`, `source`,
`specversion`, or `type` is absent; `gap` for `Event Data` if payload encoding
and schema are not stated; and `gap` for `Protocol Binding` if delivery mode is
unstated.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
