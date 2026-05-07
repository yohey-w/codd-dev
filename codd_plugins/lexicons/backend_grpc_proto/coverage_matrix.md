# proto3 and gRPC Coverage Matrix

Source: Protocol Buffers Language Guide (proto3) and gRPC core concepts.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `service` | RPC interface and methods | The material declares service, rpc, and returns contracts or states how service definitions are supplied. | RPC endpoints are mentioned without service or method contracts. |
| `message` | Request and response schemas | Message names, fields, field numbers, cardinality, oneof, or map behavior are explicit where payload shape matters. | Payloads are prose-only or omit field identity and cardinality. |
| `Scalar Value Types` | Field primitive encoding | Scalar field types such as int32, string, bool, or bytes are declared. | Values are named but their proto scalar type is absent. |
| `Enumerations` | Symbolic value sets | Enum values, default zero value, aliasing, or reserved values are declared where constrained values appear. | A finite status/type set exists but enum semantics are absent. |
| `stream` | RPC streaming lifecycle | Unary versus server-streaming, client-streaming, or bidirectional streaming behavior is explicit. | Data flow may be long-lived or incremental but stream direction is unknown. |
| `status code` | RPC completion result | Status code, status details, status message, and failure mapping are described. | Error handling is described without gRPC status semantics. |
| `Deadlines/Timeouts` | RPC wait limit and cancellation | Deadline, timeout, cancellation, or DEADLINE_EXCEEDED behavior is explicit. | Long-running calls lack termination or time budget rules. |
| `Metadata` | Call-scoped key-value context | Initial metadata, trailing metadata, binary metadata, reserved prefixes, or auth details are explicit. | Headers, trailers, or call context are implied but not specified. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
