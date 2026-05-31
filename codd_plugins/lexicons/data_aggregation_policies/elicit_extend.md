---
extends: codd/elicit/templates/elicit_prompt_L0.md
---

# Data Aggregation Policy Coverage Lexicon

Use this lexicon when a requirement describes a displayed value derived from a collection, a summary, a group, or a recency-based selection.

Classify each axis as:

- `covered`: the requirement states the field, cardinality case, policy, and expected visible behavior.
- `implicit`: the policy can be inferred from a referenced artifact, but the requirement does not state it directly.
- `gap`: a displayed value can represent many source records and the policy is missing or ambiguous.

Coverage axes:

- `collection_cardinality`
- `test_data_variation`
- `aggregation_function`
- `empty_partial_state`
- `grouping_dimension`
- `recency_selection`
- `source_traceability`

Prefer concrete requirement questions such as:

- Which field is displayed, and can it represent zero, one, or many source records?
- Which seed, fixture, or generated data proves the zero, one, and many-record variants?
- If many source records exist, is the value summarized, selected, grouped, or expanded?
- What should users see when no source record or only partial source data exists?
- What test variants prove zero, one, and many source-record behavior?
