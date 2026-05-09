---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: api_rate_limiting_caching
observation_dimensions: 7
---

# API Rate Limiting and Caching Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the 7 API platform axes declared in `lexicon.yaml`. Use HTTP and API
platform terms for rate limits, quotas, 429 responses, cache headers,
conditional requests, edge caching, and idempotent retries.

1. `rate_limit_strategy`
2. `quota_management`
3. `throttling_response`
4. `cache_control_headers`
5. `etag_conditional_requests`
6. `cdn_edge_caching`
7. `idempotency_keys`

For every axis, classify coverage as:

- `covered`: the material explicitly states the API behavior, header, status
  code, quota, cache policy, invalidation behavior, or retry-safe mutation
  mechanism and gives enough detail to verify it.
- `implicit`: the material refers to a shared API platform standard that is
  available in the same source set and clearly covers the axis.
- `gap`: the material omits API platform behavior needed to judge abuse
  prevention, client retry behavior, cache correctness, or duplicate side-effect
  prevention.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing API behavior requires human
confirmation. Severity follows `severity_rules.yaml`.

## Coverage-check examples

### covered

Requirement: "Public API requests are limited per authenticated user and tenant.
Quota exhaustion returns 429 with Retry-After. Read endpoints define
Cache-Control, ETag, and If-None-Match behavior, CDN cache invalidation occurs on
publish, and mutation POST requests require Idempotency-Key."

Classification: `covered` for all axes because rate limits, quota response,
caching, conditional validation, edge invalidation, and retry-safe mutation
behavior are explicit.

### implicit

Requirement: "The billing API follows the attached `api-platform-standard-v5`,
which defines quotas, cache headers, edge cache purge, and idempotency keys."

Classification: `implicit` for `quota_management`, `cache_control_headers`,
`cdn_edge_caching`, and `idempotency_keys` when the referenced standard is
available in the same source set.

### gap

Requirement: "The endpoint is public and clients may retry failed payment
requests."

Classification: `gap` for `rate_limit_strategy`, `throttling_response`, and
`idempotency_keys` because the material does not define abuse controls,
throttling response, or duplicate mutation protection.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
