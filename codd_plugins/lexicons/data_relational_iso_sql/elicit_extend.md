---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: data_relational_iso_sql
observation_dimensions: 10
---

# Relational ISO SQL Coverage Lexicon

Apply the base elicitation prompt, then inspect the material through the 10
ISO/IEC 9075 SQL axes declared in `lexicon.yaml`. Use only SQL standard terms
for relational coverage. Do not infer implementation-specific storage,
optimizer, driver, or dialect behavior from product names.

1. `schema definition`
2. `table definition`
3. `data types`
4. `domain constraints`
5. `referential constraints`
6. `query expressions`
7. `data change statements`
8. `transaction statements`
9. `isolation levels`
10. `views`

For every axis, classify coverage as:

- `covered`: the requirement explicitly states the SQL object, statement,
  constraint, transaction, or query expectation that owns the axis.
- `implicit`: the requirement refers to an attached SQL standard, schema, or
  design contract that is available in the same material and clearly covers the
  axis.
- `gap`: the material omits the relational data detail required to judge the
  axis.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when the missing SQL behavior requires human
confirmation. Severity follows `severity_rules.yaml`; referential integrity and
transaction gaps are usually `critical`, while schema, table, type, constraint,
query, mutation, and isolation gaps are usually `high`.

## Coverage-check examples

### covered

Requirement: "The accounting schema defines `CREATE TABLE invoice` with
`PRIMARY KEY`, `NOT NULL` amount, `FOREIGN KEY` customer_id `REFERENCES`
customer, and each posting transaction ends with `COMMIT` or `ROLLBACK`."

Classification: `covered` for `schema definition`, `table definition`,
`domain constraints`, `referential constraints`, and `transaction statements`.

### implicit

Requirement: "The attached logical data model is authoritative for SQL table
definitions and declares each table's keys, references, and transaction
boundaries."

Classification: `implicit` for `table definition`, `domain constraints`,
`referential constraints`, and `transaction statements` when the attached model
is present and contains those details.

### gap

Requirement: "Orders are saved with a customer and later shown in newest-first
order."

Classification: `gap` for `referential constraints` if the relationship has no
`FOREIGN KEY` or `REFERENCES` expectation, and `gap` for `query expressions` if
the newest-first ordering is not tied to an `ORDER BY` field.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
