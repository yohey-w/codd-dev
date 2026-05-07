# JSON Schema 2020-12 Coverage Matrix

Source: JSON Schema 2020-12 Core and Validation.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `JSON Schema Documents` | Schema document shape | The material declares a JSON Schema document, schema object, or boolean schema expectation. | Document validation exists but no schema container or media type is identified. |
| `Schema Vocabularies` | Keyword set semantics | Required or optional vocabularies and dialect expectations are explicit. | Keywords are used without the vocabulary that defines their semantics. |
| `Meta-Schemas` | Schema validation and dialect | `$schema` or the governing meta-schema is declared. | The dialect or meta-schema for the schema is unknown. |
| `Identifiers` | Resource identity | `$id`, `$anchor`, or `$dynamicAnchor` behavior is declared where references depend on it. | References rely on base URI or anchors that are not specified. |
| `References` | Schema reuse and dereferencing | `$ref`, `$dynamicRef`, and `$defs` usage is explicit where schemas are shared. | Reusable or external schemas are mentioned without dereference rules. |
| `Applicators` | Subschema application | Composition, conditionals, arrays, and object child schemas use declared applicators. | Complex structures are prose-only or omit how subschemas apply. |
| `Assertions` | Boolean validation | Type, enum, const, numeric, string, or object assertions are explicit. | Instance validity cannot be judged from stated constraints. |
| `Annotations` | Application metadata | Title, description, default, examples, or other annotations are available where needed. | Human-readable or application metadata is absent from a schema that needs it. |
| `Validation Keywords` | Structural constraints | Object and array validation keywords such as required, properties, items, and dependencies are declared. | Required fields, additional properties, tuple shape, or dependencies are ambiguous. |
| `Format` | Semantic string format | Format annotation or assertion behavior is explicit for formatted strings. | Email, URI, date-time, or other semantic strings lack format handling expectations. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
