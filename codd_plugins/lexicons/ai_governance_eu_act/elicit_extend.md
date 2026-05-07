---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: ai_governance_eu_act
observation_dimensions: 10
---

# EU AI Act Governance Observation Dimensions

Apply the base elicitation prompt, then inspect requirements and design notes
through these EU AI Act dimensions:

1. `Classification rules for high-risk AI systems`
2. `Prohibited AI practices`
3. `Requirements for high-risk AI systems`
4. `Human oversight`
5. `Transparency obligations for providers and deployers of certain AI systems`
6. `General-purpose AI models`
7. `Conformity assessment`
8. `Post-market monitoring by providers and post-market monitoring plan`
9. `Fundamental rights impact assessment for high-risk AI systems`
10. `Governance at Union and national level`

For each dimension, classify coverage as:

- `covered`: requirements explicitly state the EU AI Act obligation, risk class,
  workflow, evidence artifact, or documented non-applicability.
- `implicit`: the described feature is not an AI system, not high-risk, not GPAI,
  or not in the relevant provider/deployer context, and the non-applicability is
  clear from the material.
- `gap`: AI functionality exists and the dimension can affect legality,
  fundamental rights, safety, transparency, or lifecycle controls.

Emit findings only for dimensions classified as `gap`. Include the dimension in
`details.dimension`, the AI feature or omission signal in `details.evidence`,
and a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: A hiring-screening AI design states Annex III high-risk
  classification, Article 5 exclusion checks, risk management, training-data
  governance, human reviewer workflow, FRIA, conformity assessment, and
  post-market monitoring; classify the high-risk dimensions as `covered`.
- `implicit`: A deterministic invoice-number formatter uses no model,
  inference, profiling, or generated content; classify the AI Act dimensions as
  `implicit`.
- `gap`: A chatbot generates user-facing legal guidance with a general-purpose
  model but lacks AI disclosure, model documentation, risk classification, and
  monitoring; classify `Transparency obligations for providers and deployers of
  certain AI systems`, `General-purpose AI models`, `Classification rules for
  high-risk AI systems`, and `Post-market monitoring by providers and
  post-market monitoring plan` as `gap`.

Use recommended kinds as guidance. Do not invent dimensions outside the ten
listed above.
