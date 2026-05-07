---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: api_rest_openapi
observation_dimensions: 15
---

# API REST OpenAPI Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through the 15
OpenAPI and HTTP API axes declared in `lexicon.yaml`. Use OpenAPI 3.1 object
names for API contract coverage and HTTP/RFC terms for runtime behavior such as
content negotiation, idempotency, problem details, caching, rate limiting, and
CORS.

1. `openapi`
2. `info.version`
3. `problem_details`
4. `query`
5. `security`
6. `schemas`
7. `content`
8. `responses`
9. `links`
10. `idempotent`
11. `429`
12. `Access-Control-Allow-Origin`
13. `Cache-Control`
14. `parameters`
15. `callbacks`

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the contract expectation and the
  HTTP behavior or OpenAPI object that owns it.
- `implicit`: the requirement refers to a shared API standard that is present in
  the same material and clearly covers the axis.
- `gap`: the material omits an API contract detail required to judge the axis.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing API behavior requires human
confirmation. Severity follows `severity_rules.yaml`; authentication,
schema, status-code, media type, CORS, and parameter gaps are usually `high` or
`critical`, while versioning, pagination, cache, link, idempotency, rate-limit,
and async gaps are usually `medium` unless the material makes them
release-blocking.

## Coverage-check examples

### covered

Requirement: "The account API is published as OpenAPI 3.1, every operation has
`responses` for 200, 400, 401, 403, 404, and default, and errors use
`application/problem+json`."

Classification: `covered` for `openapi`, `responses`, and `problem_details`
because the document shape, status-code map, and machine-readable error format
are explicit.

### implicit

Requirement: "All public endpoints follow the shared API platform standard
`api-standard-v4`, which is attached and defines OAuth2 security schemes,
JSON schemas, CORS headers, cache policy, and 429 Retry-After handling."

Classification: `implicit` for `security`, `schemas`,
`Access-Control-Allow-Origin`, `Cache-Control`, and `429` when the referenced
standard is available in the same source set and covers those details.

### gap

Requirement: "The service returns a list of invoices and the frontend loads
more results as the user scrolls."

Classification: `gap` for `query` when the material does not specify cursor or
limit parameters, next-page links, default page size, or end-of-list behavior.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
