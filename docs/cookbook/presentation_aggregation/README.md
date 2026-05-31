# Presentation And Aggregation Obligations

Use this pattern when a user journey displays fields whose meaning depends on locale, timezone, number formatting, or a many-to-one data policy.

```yaml
user_journeys:
  - name: review_record_overview
    criticality: high
    steps:
      - { action: navigate, target: /records }
      - { action: expect_visible, value: record_summary_many_source_display }
    expected_outcome_refs: [lexicon:record_overview_display]
    display_fields:
      - field_id: "record.published_at"
        data_type: "datetime"
        lexicon_refs: ["i18n_unicode_cldr#time_zone_handling"]
        presentation_required: true
        presentation:
          format: "YYYY-MM-DD HH:mm"
          timezone: "Etc/UTC"
          locale: "en-US"
        expected_presentation_signals: ["record_published_at_locale_display"]
      - field_id: "record.summary_value"
        cardinality: "0..N"
        aggregation_required: true
        aggregation:
          cardinality_when_zero: { display: "empty_state" }
          cardinality_when_one: { display: "raw" }
          cardinality_when_many:
            policy: "average"
            source_traceability: "source_count"
          test_data_variants:
            required_cardinality: ["0", "1", "N"]
        expected_aggregation_signals: ["record_summary_many_source_display"]
```

The important contract is that each displayed field has three matching parts: the obligation, an assertion signal, and test data that exercises the required variant set.
