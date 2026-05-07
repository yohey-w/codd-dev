# CloudEvents 1.0.2 Coverage Matrix

Source: CloudEvents specification version 1.0.2.

| Axis | Covered when | Implicit when | Gap when |
| --- | --- | --- | --- |
| `Context Attributes` | Context metadata, attribute naming, and inspection expectations are specified. | The requirement explicitly consumes an already validated CloudEvent envelope. | The event must interoperate but context metadata is absent or vague. |
| `REQUIRED Attributes` | `id`, `source`, `specversion`, and `type` are specified or explicitly inherited. | A referenced CloudEvents profile already defines all required attributes. | The event envelope lacks one or more required attributes. |
| `OPTIONAL Attributes` | Payload content type, schema, subject, and time decisions are specified when relevant. | The event has no payload or the profile explicitly excludes optional attributes. | Payload or timing behavior matters but optional attributes are not addressed. |
| `Type System` | Attribute types and canonical string conversions are specified or referenced. | The implementation delegates fully to a CloudEvents SDK with no custom attributes. | Custom or required attributes are named but their types are ambiguous. |
| `Extension Context Attributes` | Extension names, types, semantics, and collision behavior are specified. | No non-standard metadata is required. | Correlation, identity, or routing metadata is needed but extension treatment is absent. |
| `Event Data` | Payload shape, media type, schema, and optionality are specified. | The event is intentionally metadata-only and states no `data` value is present. | Domain payload is required but its encoding or schema is missing. |
| `Protocol Binding` | Transport mapping, binary/structured mode, and event format expectations are specified. | The requirement is strictly in-memory and does not serialize or transport events. | Events cross a boundary but transport mapping or message mode is unstated. |

Reviewers classify each axis as `covered`, `implicit`, or `gap`. Findings are
emitted only for `gap`.
