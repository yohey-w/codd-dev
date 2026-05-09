---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: ddd_domain_driven_design
observation_dimensions: 9
---

# Domain-Driven Design Coverage Lexicon

Apply the base elicitation prompt, then inspect medium-to-large business
application requirements through these DDD strategic and tactical axes:

1. `ubiquitous_language`
2. `bounded_context`
3. `aggregate_design`
4. `entity_value_object`
5. `domain_events`
6. `repository_pattern`
7. `application_service`
8. `context_mapping`
9. `anti_corruption_layer`

For each axis, classify coverage as:

- `covered`: requirements explicitly state the relevant DDD modeling decision or
  pattern boundary.
- `implicit`: the axis is not independently relevant because the described
  feature is too small, CRUD-only, or fully delegated to an existing model.
- `gap`: the axis can affect correctness or maintainability and no expectation
  is specified.
- `not_found`: no evidence for the axis exists in the reviewed material when
  downstream scoring needs a missing-modeling status distinct from `gap`.

Emit findings only for `gap` or `not_found` axes. Include the axis in
`details.dimension`, the evidence or omission signal in `details.evidence`, and
a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: The requirement defines Order and Invoice as separate bounded
  contexts, names their upstream/downstream relationship, and states that Order
  is modified only through an Order aggregate root; classify `bounded_context`,
  `context_mapping`, and `aggregate_design` as `covered`.
- `implicit`: A static admin lookup table has no domain behavior beyond simple
  CRUD and inherits an existing model; classify `domain_events` and
  `anti_corruption_layer` as `implicit`.
- `gap`: A payment workflow changes customer status, invoice state, and rewards
  balance in one operation but no aggregate or invariant boundary is defined;
  classify `aggregate_design` as `gap`.
- `not_found`: A specification for a multi-team business application contains no
  shared vocabulary, bounded context, aggregate, or context map evidence; emit
  `not_found` findings for the applicable DDD axes.

Use recommended kinds as guidance. Do not invent additional DDD axes outside
the nine listed above.
