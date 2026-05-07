---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: ops_observability_otel
observation_dimensions: 8
---

# OpenTelemetry Observability Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the 8 OpenTelemetry observability axes declared in `lexicon.yaml`.
Use OpenTelemetry specification terms for telemetry signals, propagation,
resources, instrumentation, Collector pipelines, and semantic conventions.

1. `signals_traces`
2. `signals_metrics`
3. `signals_logs`
4. `context_propagation`
5. `resource_attributes`
6. `instrumentation`
7. `collector`
8. `semantic_conventions`

For every axis, classify coverage as:

- `covered`: the material explicitly states the telemetry expectation and the
  OpenTelemetry component, signal, or convention that owns it.
- `implicit`: the material references a shared OpenTelemetry baseline that is
  present in the same source set and clearly covers the axis.
- `gap`: the material omits an observability contract needed to judge telemetry
  generation, correlation, identity, processing, or vocabulary consistency.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing observability behavior requires human
confirmation. Severity follows `severity_rules.yaml`; propagation gaps are
usually `critical`, trace, metric, resource, instrumentation, collector, and
semantic-convention gaps are usually `high`, and log gaps are usually `medium`
unless the material makes logs release-blocking.

## Coverage-check examples

### covered

Requirement: "The checkout service emits OpenTelemetry `Spans` for each
external call, propagates `SpanContext` and `Baggage` with `TextMapPropagator`,
sets `service.name`, and sends traces through a Collector `pipelines` entry."

Classification: `covered` for `signals_traces`, `context_propagation`,
`resource_attributes`, and `collector` because the signal, propagation carrier,
resource identity, and Collector path are explicit.

### implicit

Requirement: "All services follow the attached platform OTel baseline
`otel-standard-v2`, which defines metrics instruments, log records, resource
attributes, instrumentation scope, Collector pipelines, and semantic convention
groups."

Classification: `implicit` for `signals_metrics`, `signals_logs`,
`resource_attributes`, `instrumentation`, `collector`, and
`semantic_conventions` when the referenced baseline is available in the same
source set and covers those details.

### gap

Requirement: "The worker exposes health telemetry and the dashboard groups it
by service and tenant."

Classification: `gap` for `signals_metrics`, `resource_attributes`, and
`semantic_conventions` when the material does not specify metric instruments,
resource identity such as `service.name`, or standard attribute naming.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
