# Domain-Driven Design Coverage Matrix

Source: Eric Evans, Domain-Driven Design, and Vaughn Vernon, Implementing
Domain-Driven Design.

| Axis | Covered when | Implicit when | Gap or not_found when |
| --- | --- | --- | --- |
| `ubiquitous_language` | Shared domain terms, definitions, and naming expectations are stated. | The feature is purely technical and has no business vocabulary. | Business terms appear but their meaning or code/model usage is unstated. |
| `bounded_context` | Model boundaries, ownership, and language scope are explicit. | The change stays inside one already named context. | Multiple meanings, teams, products, or subdomains are involved without a boundary. |
| `aggregate_design` | Aggregate root, invariant boundary, and transaction scope are stated. | The feature has no mutable domain consistency rule. | State changes can break business invariants but no aggregate boundary exists. |
| `entity_value_object` | Identity-bearing entities and value-equality objects are distinguished. | The feature has no persistent domain objects or value semantics. | Objects have lifecycle, equality, or mutation rules but no semantic distinction. |
| `domain_events` | Significant state changes and event publication/handling behavior are stated. | No other process or model needs to react to state changes. | A business occurrence matters outside the command but event behavior is absent. |
| `repository_pattern` | Aggregate loading/saving abstraction and persistence boundaries are stated. | Persistence is entirely outside the feature or already covered by an existing repository. | Use cases need aggregate persistence but storage access boundaries are unclear. |
| `application_service` | Use case orchestration, transaction, authorization, and coordination boundaries are stated. | The feature is a simple domain operation already covered by an existing service. | Workflow coordination exists but its boundary is mixed with domain rules or UI code. |
| `context_mapping` | Upstream/downstream, ACL, shared kernel, or other context relationships are stated. | No other bounded context or external model participates. | Multiple models integrate but relationship and dependency direction are unstated. |
| `anti_corruption_layer` | Translation and isolation from legacy, vendor, or external models are stated. | No legacy or external model enters the domain model. | External concepts cross into the core model without a protective translation boundary. |

Reviewers classify each axis as `covered`, `implicit`, `gap`, or `not_found`.
Findings are emitted for `gap` and `not_found`.
