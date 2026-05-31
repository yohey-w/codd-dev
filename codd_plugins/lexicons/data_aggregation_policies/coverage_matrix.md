# Data Aggregation Policies Coverage Matrix

| Axis | Covered | Implicit | Gap |
|------|---------|----------|-----|
| `collection_cardinality` | Zero, one, and many source-record cases are stated. | Cardinality is inferable from a schema or API shape. | A displayed value can collapse many records without stated behavior. |
| `test_data_variation` | Seed, fixture, or generated data covers zero, one, and many-record variants. | The data generator implies variants but does not name them. | Tests can run without data that exercises the declared cardinality cases. |
| `aggregation_function` | The summary, selection, grouping, or expansion policy is declared. | A named artifact implies the policy. | The display gives one value but does not state how it was derived. |
| `empty_partial_state` | Empty and partial placeholders or fallbacks are declared. | Empty behavior is inherited from a shared UI convention. | Missing source data can look like a valid value. |
| `grouping_dimension` | Group key, order, and labels are declared. | Grouping is inherited from a referenced component contract. | Grouped output lacks a declared dimension or order. |
| `recency_selection` | Timestamp source, window, and tie-breaker are declared. | Recency behavior is implied by a named query. | Latest or windowed display lacks source or tie-break policy. |
| `source_traceability` | Source count, drilldown, or calculation label is declared. | Traceability exists in linked diagnostics only. | A summary cannot be explained or verified from declared behavior. |
