---
extends: codd/elicit/templates/elicit_prompt_L0.md
lexicon_name: mobile_ios_hig
observation_dimensions: 12
---

# Apple HIG Mobile Observation Dimensions

Apply the base elicitation prompt, then inspect the material through these
Apple Human Interface Guidelines dimensions:

1. `Navigation and search`
2. `Typography`
3. `Color`
4. `Accessibility`
5. `Playing haptics`
6. `Motion`
7. `Inputs`
8. `Layout`
9. `Icons`
10. `Playing audio`
11. `Privacy`
12. `Feedback`

For each dimension, classify coverage as:

- `covered`: requirements explicitly state the expected behavior for this
  dimension.
- `implicit`: the dimension is not independently relevant because the described
  feature does not expose that behavior or delegates it entirely to a platform
  default.
- `gap`: the dimension can affect user-visible behavior and no expectation is
  specified.

Emit findings only for dimensions classified as `gap`. Include the dimension in
`details.dimension`, the evidence or omission signal in `details.evidence`, and
a reviewer-facing question in `question`.

## Coverage-check examples

- `covered`: The requirement says the settings flow exposes search, empty
  results, and a clear back path; classify `Navigation and search` as
  `covered`.
- `implicit`: A static legal text screen has no animation, tactile feedback, or
  audio; classify `Motion`, `Playing haptics`, and `Playing audio` as
  `implicit`.
- `gap`: A checkout action can fail and can delete saved payment data, but no
  alert, confirmation, or recovery copy is described; classify `Feedback` as
  `gap`.

Use recommended kinds as guidance. Do not invent additional dimensions outside
the twelve listed above.
