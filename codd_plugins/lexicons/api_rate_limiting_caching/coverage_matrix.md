# API Rate Limiting and Caching Coverage Matrix

Source: RFC 6585, RFC 7234, RFC 9110, RFC 9111, and common API platform
practice.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `rate_limit_strategy` | Rate subject and enforcement | Per-user, per-IP, per-tenant, or equivalent rate limiting scope and window is defined. | Public request volume can grow without a declared limit strategy. |
| `quota_management` | Plan or period usage limits | Daily, monthly, plan, or tenant quotas and overage behavior are declared. | Consumers do not know usage ceilings or exhaustion behavior. |
| `throttling_response` | Client-visible throttling | 429 Too Many Requests and retry guidance such as Retry-After are specified. | Clients cannot predict how to back off after throttling. |
| `cache_control_headers` | HTTP cache policy | Cache-Control directives such as max-age, private, no-cache, or no-store are declared. | Responses can be cached incorrectly or not cached when intended. |
| `etag_conditional_requests` | Representation validation | ETag, If-None-Match, or 304 Not Modified behavior is defined. | Clients cannot validate cached representations safely. |
| `cdn_edge_caching` | Shared or edge cache lifecycle | CDN cacheability, surrogate policy, and invalidation are specified. | Edge caches can serve stale objects or miss cacheable content. |
| `idempotency_keys` | Retry-safe mutations | Idempotency keys or duplicate request controls protect side-effecting operations. | Retried mutations can create duplicate payments, orders, or writes. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.
