---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: ml_model_cards
observation_dimensions: 8
---

# ML Model Cards Coverage Lexicon

Apply the base elicitation prompt, then inspect requirements and design notes
through the 8 model documentation axes declared in `lexicon.yaml`. Use Model
Cards terminology for model details, intended use, factors, metrics, data,
ethics, caveats, and lifecycle traceability.

1. `model_details`
2. `intended_use`
3. `evaluation_factors`
4. `performance_metrics`
5. `training_data`
6. `ethical_considerations`
7. `caveats_recommendations`
8. `model_versioning`

For every axis, classify coverage as:

- `covered`: the material explicitly states the model-card information, metric,
  factor, data, risk, limitation, or lifecycle mechanism and gives enough detail
  to verify it.
- `implicit`: the material refers to a shared model-card, AI governance, or ML
  lifecycle standard that is available in the same source set and clearly covers
  the axis.
- `gap`: the material omits model documentation needed to judge safe,
  transparent, and reproducible model use.

Emit findings only for `gap` axes. Populate `details.dimension` with the axis
identifier from `lexicon.yaml`, include `details.evidence`, and ask a
reviewer-facing question when missing model documentation requires human
confirmation. Severity follows `severity_rules.yaml`; intended use and ethical
considerations gaps are high severity by default.

## Coverage-check examples

### covered

Requirement: "The fraud model card lists the model architecture, version,
training date, intended use for transaction risk scoring, out-of-scope use for
credit decisions, validation factors, precision/recall thresholds, training data
summary, fairness analysis, limitations, and rollback plan."

Classification: `covered` for all axes because the model-card content is
explicit and verifiable.

### implicit

Requirement: "The service must publish model documentation according to the
attached `model-card-standard-v1`, which defines factors, metrics, ethical
considerations, and caveats."

Classification: `implicit` for `evaluation_factors`, `performance_metrics`,
`ethical_considerations`, and `caveats_recommendations` when the referenced
standard is available in the same source set.

### gap

Requirement: "The application calls an LLM to classify support tickets and uses
the result to route urgent issues."

Classification: `gap` for `model_details`, `intended_use`,
`performance_metrics`, `training_data`, and `ethical_considerations` because the
material does not define the model provenance, use boundary, evaluation,
underlying data, or risk analysis.

Do not invent axes outside `lexicon.yaml`. Findings outside this lexicon should
set `severity: info` and `details.note: "outside_lexicon_scope"`.
