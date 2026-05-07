# API REST OpenAPI Coverage Matrix

Source: OpenAPI Specification 3.1.0 plus HTTP API semantics from RFC 7231,
RFC 7807, RFC 9110, RFC 9111, RFC 6585, and Fetch CORS.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `openapi` | OpenAPI document shape | The contract declares `openapi` and at least one of `paths`, `components`, or `webhooks`. | API behavior is described only in prose or examples. |
| `info.version` | API versioning | Contract versioning and compatibility rules are explicit. | The API can change without version metadata or migration expectations. |
| `problem_details` | Error model | Error bodies use a documented problem format with status alignment. | Errors are ad hoc strings or undocumented objects. |
| `query` | Pagination | Collection endpoints define query controls, limits, and next-page traversal. | A list endpoint exists without page size, cursor, or continuation behavior. |
| `security` | Authentication and authorization | Security requirements and schemes are named per operation or globally. | Protected operations lack security requirements or scheme definitions. |
| `schemas` | Request and response shape | Payloads and reusable models have schema contracts. | Payload shape, required fields, or type constraints are absent. |
| `content` | Media types and negotiation | Request and response media types and negotiation behavior are explicit. | Clients cannot tell which representations are accepted or returned. |
| `responses` | HTTP status code map | Success, client error, server error, and fallback responses are documented. | Only a happy path is specified. |
| `links` | Hypermedia relations | Related operations use links or an equivalent documented relation contract. | Clients must infer follow-up operations from prose or naming. |
| `idempotent` | Retry semantics | PUT, DELETE, safe methods, or idempotency keys have explicit retry behavior. | Clients cannot safely retry failed calls. |
| `429` | Rate limiting | 429 and retry timing are documented when throttling exists. | Quotas or throttling exist without client-facing retry guidance. |
| `Access-Control-Allow-Origin` | Browser cross-origin sharing | Origin, credentials, methods, and headers are documented for browser clients. | Browser access is required but CORS policy is unspecified. |
| `Cache-Control` | Caching and validation | Cache directives, validators, and 304 handling are explicit where applicable. | Cacheable or sensitive responses have no cache policy. |
| `parameters` | Parameter validation | Path, query, header, and cookie parameters declare required, schema, and serialization rules. | Inputs are named without location, type, bounds, or encoding. |
| `callbacks` | Async HTTP operations | Callbacks, webhooks, callback URLs, payloads, and retries are documented. | Async side effects exist without a callback or webhook contract. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
