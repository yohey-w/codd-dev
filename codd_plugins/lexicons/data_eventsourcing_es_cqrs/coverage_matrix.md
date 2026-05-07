# Event Sourcing and CQRS Coverage Matrix

Sources: Martin Fowler Event Sourcing and Microsoft Learn Event Sourcing
pattern.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `Event Sourcing` | State changes are captured as a sequence of events and state derivation is specified. | The material explicitly declares the feature outside event-sourced boundaries. | The system claims event sourcing but the state-change model is absent. |
| `Event` | Event names, business intent, payload meaning, and correction behavior are specified. | Events are imported from a referenced canonical domain event catalog. | Events are mentioned but their meaning or intent is not defined. |
| `event store` | Persistence, append-only behavior, ordering, uniqueness, and source-of-record rules are specified. | The event store contract is fully defined in an attached platform profile. | Event persistence is required but storage semantics are unstated. |
| `Event Replay` | Replay order, correction, idempotency, and side-effect control are specified. | Replay is explicitly out of scope for the bounded context. | Rebuild, audit, or repair depends on replay but replay behavior is absent. |
| `Application State Storage` | Snapshot, current-state cache, event log derivation, and recovery behavior are specified. | Current state is never cached and all reads explicitly replay the log. | Current state is used but snapshot or derivation behavior is missing. |
| `Complete Rebuild` | Rebuild trigger, source, side-effect suppression, and operational constraints are specified. | Complete rebuild is explicitly unsupported and a separate recovery process is defined. | Rebuild is expected for migration or recovery but not specified. |
| `Temporal Query` | Past-state query semantics, event range, and query time behavior are specified. | Historical reads are explicitly excluded from requirements. | Audit or history needs exist but temporal query behavior is absent. |
| `CQRS pattern` | Read/write separation, materialized views, projection lag, and consistency expectations are specified. | A single consistent model is explicitly required and projections are not used. | Projections or read models are mentioned but consistency and separation are unclear. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.
