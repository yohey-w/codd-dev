---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: backend_graphql
observation_dimensions: 11
---

# GraphQL October 2021 Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through the 11
GraphQL October 2021 axes declared in `lexicon.yaml`. Use GraphQL specification
terms literally. Do not infer a server library, gateway, vendor composition profile, ORM,
client framework, or vendor behavior unless the material states it. GraphQL
October 2021 type system extensions are in scope; vendor composition constructs
are outside this lexicon unless separately supplied by another plugin.

1. `schema`
2. `Type System`
3. `Type System Extensions`
4. `query`
5. `mutation`
6. `subscription`
7. `Fragments`
8. `Variables`
9. `Directives`
10. `Introspection`
11. `Response`

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the GraphQL construct that owns
  the axis, such as schema, type system, type system extension, query, mutation,
  subscription, fragment, variable, directive, introspection field, response
  data, or response errors.
- `implicit`: the requirement refers to an attached GraphQL SDL, operation
  document, or API contract that is present in the same material and clearly
  covers the axis.
- `gap`: the material omits the GraphQL detail required to judge the axis.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing GraphQL behavior needs human
confirmation. Severity follows `severity_rules.yaml`; schema, type system,
query, mutation, variables, and response gaps are usually `high`, while
subscription, fragments, directives, introspection, and extension gaps are
usually `medium` unless the material makes them release-blocking.

## Coverage-check examples

### covered

Requirement: "The GraphQL schema defines `type Query { user(id: ID!): User }`,
`type Mutation { updateUser(input: UpdateUserInput!): User! }`, input objects,
`@deprecated`, fragments for User fields, variables, and response `errors`."

Classification: `covered` for `schema`, `Type System`, `query`, `mutation`,
`Variables`, `Directives`, `Fragments`, and `Response`.

### implicit

Requirement: "All product API operations use the attached schema.graphql and
operations.graphql documents, which include root operation types, type
extensions, variables, directives, subscriptions, introspection policy, and
response error handling."

Classification: `implicit` for all axes when the attached SDL and operation
documents are present and contain those details.

### gap

Requirement: "Clients can read orders, change order status, and subscribe to
shipment updates."

Classification: `gap` for `schema`, `query`, `mutation`, and `subscription` if
the GraphQL schema and operation documents are absent, and `gap` for `Response`
if error payload behavior is not stated.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
