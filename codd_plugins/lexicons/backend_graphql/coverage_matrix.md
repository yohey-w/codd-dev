# GraphQL October 2021 Coverage Matrix

Source: GraphQL Specification October 2021.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `schema` | Root operation types | Schema, query, mutation, or subscription root operation types are explicit. | API capabilities are described without a schema or root operation mapping. |
| `Type System` | GraphQL type contracts | Scalar, object, interface, union, enum, input, list, and non-null behavior are declared where relevant. | Data shapes are prose-only or omit GraphQL type definitions. |
| `Type System Extensions` | Schema and type extension | `extend schema`, `extend type`, or related extension behavior is explicit. | Extension-based composition is implied but no GraphQL extension construct is stated. |
| `query` | Read operation | Query operation, fields, selection set, arguments, and aliases are explicit. | Read behavior is described without fields or selection semantics. |
| `mutation` | Write operation | Mutation root field, input object, result fields, and serial execution expectations are explicit. | Write behavior lacks GraphQL mutation contract details. |
| `subscription` | Event operation | Subscription operation and response stream/source event stream behavior are explicit. | Real-time behavior is mentioned without GraphQL subscription semantics. |
| `Fragments` | Reusable selections | Named fragments, inline fragments, fragment spreads, or type conditions are explicit. | Reuse or polymorphic selection is implied but fragment behavior is absent. |
| `Variables` | Operation inputs | Variable definitions, uses, default values, and input type constraints are explicit. | Operation parameters are present but GraphQL variable rules are absent. |
| `Directives` | Annotations and conditional execution | Directive definitions or directives such as `@include`, `@skip`, or `@deprecated` are explicit. | Conditional selection or annotations are implied but directive behavior is absent. |
| `Introspection` | Schema discoverability | Introspection fields or types such as `__schema`, `__type`, or `__typename` are explicit. | Tooling or discovery is expected but introspection behavior is unspecified. |
| `Response` | Data and error result format | `data`, `errors`, `extensions`, request errors, and field errors are defined where results matter. | Client error handling or partial responses are described without GraphQL response semantics. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
