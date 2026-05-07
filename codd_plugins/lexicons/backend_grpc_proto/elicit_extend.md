---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: backend_grpc_proto
observation_dimensions: 8
---

# proto3 and gRPC Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through the 8
proto3 and gRPC axes declared in `lexicon.yaml`. Use proto3 and gRPC terms
literally. Do not infer a code generator, framework, registry, gateway,
language binding, or transport product unless the material states it.

1. `service`
2. `message`
3. `Scalar Value Types`
4. `Enumerations`
5. `stream`
6. `status code`
7. `Deadlines/Timeouts`
8. `Metadata`

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the proto3 or gRPC construct
  that owns the axis, such as `service`, `rpc`, `message`, scalar type,
  `enum`, `stream`, status code, deadline, timeout, or metadata.
- `implicit`: the requirement refers to an attached `.proto` file or gRPC
  contract that is present in the same material and clearly covers the axis.
- `gap`: the material omits the proto3 or gRPC detail required to judge the
  axis.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing RPC behavior needs human
confirmation. Severity follows `severity_rules.yaml`; service, message, scalar
type, streaming, status, and deadline gaps are usually `high`, while enum and
metadata gaps are usually `medium` unless the material makes them
release-blocking.

## Coverage-check examples

### covered

Requirement: "Define `service SearchService` with `rpc Search(SearchRequest)
returns (SearchResponse)`, `message SearchRequest`, `string query = 1`,
`int32 page_number = 2`, a 500 ms deadline, and `NOT_FOUND` or `OK` statuses."

Classification: `covered` for `service`, `message`, `Scalar Value Types`,
`status code`, and `Deadlines/Timeouts`.

### implicit

Requirement: "The checkout RPCs use the attached orders.proto contract, which
declares services, messages, streaming imports, field numbers, enum states,
metadata, deadlines, and status mappings."

Classification: `implicit` for all axes when the attached `.proto` and gRPC
contract are present and contain those details.

### gap

Requirement: "The backend provides a live order updates RPC and returns errors
when the client is too slow."

Classification: `gap` for `stream` if the direction is not stated, `gap` for
`status code` if no gRPC status is mapped, and `gap` for
`Deadlines/Timeouts` if the time budget is absent.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
