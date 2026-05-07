---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: data_eventsourcing_es_cqrs
observation_dimensions: 8
---

# Event Sourcing and CQRS Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through the 8 Event
Sourcing and CQRS axes declared in `lexicon.yaml`. Use Fowler and Microsoft
Learn source literals. Do not infer a database engine, streaming product, cloud
service, or vendor implementation unless it is expressed by the requirement.

1. `Event Sourcing`
2. `Event`
3. `event store`
4. `Event Replay`
5. `Application State Storage`
6. `Complete Rebuild`
7. `Temporal Query`
8. `CQRS pattern`

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the event, event store,
  replay, snapshot, rebuild, temporal query, projection, or consistency behavior
  that owns the axis.
- `implicit`: the requirement references an attached event-sourced architecture
  profile that fully covers the axis, or explicitly excludes the axis from the
  bounded context.
- `gap`: the material describes event-sourced data or projections but omits the
  architecture detail needed to judge correctness or operability.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing event-sourcing behavior requires
human confirmation. Severity follows `severity_rules.yaml`.

## Coverage-check examples

### covered

Requirement: "Order aggregates append business-intent events to an event store,
build materialized views from stored events, snapshot every 500 events, and
disable external gateways during replay mode."

Classification: `covered` for `Event`, `event store`, `Application State
Storage`, `Event Replay`, and `CQRS pattern`.

### implicit

Requirement: "Billing follows the attached Event Sourcing architecture profile;
this feature only adds a read-only report over an existing materialized view."

Classification: `implicit` for `event store`, `Event Replay`, and
`Application State Storage` when the attached profile defines those details.

### gap

Requirement: "Store every account change as an event and expose a current
account balance query."

Classification: `gap` for `event store` if append-only and source-of-record
rules are absent, `gap` for `Application State Storage` if current state or
snapshot behavior is unstated, and `gap` for `CQRS pattern` if the balance query
uses projections but consistency is not described.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
