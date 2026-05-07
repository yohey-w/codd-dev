# Relational ISO SQL Coverage Matrix

Source: ISO/IEC 9075-1:2023 SQL/Framework and ISO/IEC 9075-2:2023
SQL/Foundation.

| Axis | Coverage target | Covered when | Gap signal |
| --- | --- | --- | --- |
| `schema definition` | SQL object namespace | Schema/catalog ownership and object grouping are explicit. | Tables, views, or routines are named without a schema boundary. |
| `table definition` | Stored relation shape | Tables, columns, table kind, and lifecycle are declared. | Data is referenced only as prose or informal fields. |
| `data types` | Column and expression value domains | Character, numeric, datetime, boolean, or other relevant value domains are explicit. | Fields lack types, precision, scale, or temporal semantics. |
| `domain constraints` | In-row integrity | NOT NULL, UNIQUE, CHECK, PRIMARY KEY, or equivalent constraints are declared. | Requiredness, uniqueness, keys, or value ranges are absent. |
| `referential constraints` | Cross-table integrity | FOREIGN KEY, REFERENCES, and referential actions are declared where relationships exist. | Relationships are prose-only or omit update/delete effects. |
| `query expressions` | Read semantics | SELECT/FROM/WHERE/GROUP BY/ORDER BY behavior is specified for required reads. | Consumers cannot infer projection, filters, aggregation, or ordering. |
| `data change statements` | Write semantics | INSERT, UPDATE, DELETE, or MERGE behavior is covered for mutations. | Mutations are described without row creation, mutation, removal, or merge semantics. |
| `transaction statements` | Unit-of-work boundary | START TRANSACTION, COMMIT, ROLLBACK, or equivalent boundaries are explicit. | Multiple writes happen without completion or abort behavior. |
| `isolation levels` | Concurrency semantics | Required isolation mode and concurrent read/write expectations are stated. | Concurrent behavior is business-critical but isolation is omitted. |
| `views` | Derived table interface | CREATE VIEW, viewed table, and update check behavior are declared where used. | Derived relations are mentioned without query or update constraints. |

Reviewers should classify each axis as `covered`, `implicit`, or `gap`.
Findings are emitted only for `gap`.
